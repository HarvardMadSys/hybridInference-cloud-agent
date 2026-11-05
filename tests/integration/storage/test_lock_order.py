"""Writers that meet on the same rows, interleaved on real Postgres.

The order every transaction takes shared rows in is written down once, in
:mod:`cloud_agent.storage`. A test here holds one writer part-way through -- on
a connection of its own, taking the rows in the order that writer's code does,
or by pausing the real call -- runs another real repository call into it, and
then lets the held writer go on: two orders over the same rows is a deadlock,
and Postgres says so. The attempt reaper is here too, for what a writer that
ran in between leaves it to find, and so is the event append that
is tried again when Postgres picks it as a deadlock's victim: a rival closes a
real cycle against it.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio
from typing import TYPE_CHECKING

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _expire_attempt,
    _reserve_shared_chat,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)
from tests.integration.storage.test_environment_snapshot_store import (
    _claim as _claim_environment_run,
    _ready_snapshot,
    _receipt,
)
from tests.integration.storage.test_environment_store import _save as _save_environment
from tests.integration.storage.test_sandbox_restore_store import (
    _covered,
    _state as _restore_state,
)
from tests.support.follow_ups import append_follow_up

from cloud_agent.storage._sql.sandbox import fence_sandbox_start
from cloud_agent.storage.repositories import (
    attempt_commands as attempt_module,
    session_fork as session_fork_module,
)

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_HOST = "shared-runner"
_HOST_CREDENTIAL = "test-credential-hash"


async def _run_turn(
    store: Storage,
    job: dict,
    claim: dict,
    sandbox: dict,
    *,
    context_status: str = "new",
    container_id: str = "container-lock-order",
) -> None:
    """Start, admit, settle and finish one retained turn, as its runner does."""
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id=container_id, context_status=context_status
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-lock-order",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )


async def _run_next_turn(pool, store: Storage, parent: dict, sandbox: dict) -> dict:
    """Send a follow-up into the same workspace and run it to the end."""
    follow = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id=parent["user_id"], prompt="and update the docs"
    )
    assert follow is not None and follow.get("error") is None
    claim = await _claim(
        store,
        worker_id="next-turn",
        lease_ttl_seconds=60,
        host=_HOST,
        host_credential_hash=_HOST_CREDENTIAL,
    )
    assert claim is not None and claim["id"] == follow["id"]
    reserved = await store.sandbox.reserve_thread_sandbox(
        thread_id=follow["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=_HOST,
        sandbox_id=sandbox["sandbox_id"],
        container_name=sandbox["container_name"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        sandbox_image_digest=sandbox["sandbox_image_digest"],
        execution_profile=sandbox["execution_profile"],
    )
    assert reserved is not None
    await _run_turn(store, follow, claim, reserved, context_status="live")
    return follow


async def _blocked_by(pool, pid: int) -> None:
    """Return once another backend waits on a lock that ``pid`` holds."""
    await _waiting_on(pool, pid)


async def _waiting_on(pool, pid: int) -> int:
    """The backend that waits on a lock ``pid`` holds, once one does."""
    async with asyncio.timeout(10), pool.acquire() as observer:
        while True:
            waiter = await observer.fetchval(
                "SELECT pid FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)) LIMIT 1",
                pid,
            )
            if waiter is not None:
                return waiter
            await asyncio.sleep(0.01)


async def _settle(task: asyncio.Task | None) -> None:
    """Do not leave a call behind when an assertion ends the test early."""
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _first_try_only(write: str, transaction):
    """Run a write once: the lock order alone has to keep it out of a cycle."""
    return await transaction()


async def _close_a_cycle(rival, statement: str, *args) -> None:
    """Wait for a row the waiting writer holds, which makes it a deadlock's victim.

    The writer is already waiting for a row ``rival`` holds. This side checks
    for a cycle only after a minute, so Postgres finds it from the writer's side
    and aborts the writer; this statement then gets its row.
    """
    await rival.execute("SET LOCAL deadlock_timeout = '60s'")
    await asyncio.wait_for(rival.execute(statement, *args), timeout=10)


async def test_an_event_append_picked_as_a_deadlock_victim_is_tried_again(pool, store: Storage):
    """A reply Postgres aborted to break a lock cycle is appended on a second try.

    The runner takes a refused append as a lost turn and marks the chat's
    retained context lost. The rival here takes the conversation and then the
    attempt, the reverse of the lock order.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    append = None
    async with pool.acquire() as rival:
        try:
            async with rival.transaction():
                await rival.execute(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", job["thread_id"]
                )
                append = asyncio.create_task(
                    store.attempt_commands.append_event(
                        attempt_id=claim["attempt_id"],
                        lease_generation=claim["lease_generation"],
                        event_type="message",
                        payload={"text": "Reading the failing test."},
                        event_seq=1,
                    )
                )
                await _blocked_by(pool, await rival.fetchval("SELECT pg_backend_pid()"))
                await _close_a_cycle(
                    rival,
                    "SELECT id FROM agent_attempts WHERE id = $1 FOR UPDATE",
                    claim["attempt_id"],
                )
            event_id = await asyncio.wait_for(append, timeout=10)
        finally:
            await _settle(append)
    assert event_id is not None
    async with pool.acquire() as conn:
        mirrored = await conn.fetch(
            "SELECT source_event_id FROM agent_thread_messages "
            "WHERE job_id = $1 AND role = 'assistant'",
            job["id"],
        )
    assert [row["source_event_id"] for row in mirrored] == [event_id]


async def test_cancelling_a_turn_does_not_deadlock_with_its_streaming_reply(
    pool, store: Storage, monkeypatch
):
    """A cancel and the reply being appended both take the conversation first.

    An assistant message is mirrored into the transcript, whose foreign key
    locks the turn's job, and the append bumped the conversation only after
    that. A cancel takes the conversation and then the job, so cancelling a
    turn while it streamed deadlocked with it; an append that lost marked the
    chat's retained context lost.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    held, release = asyncio.Event(), asyncio.Event()
    cancel_pid: list[int] = []
    original = attempt_module.lock_conversation_by_job

    async def hold_the_conversation(conn, *, job_id):
        thread_id = await original(conn, job_id=job_id)
        cancel_pid.append(await conn.fetchval("SELECT pg_backend_pid()"))
        held.set()
        await release.wait()
        return thread_id

    monkeypatch.setattr(attempt_module, "lock_conversation_by_job", hold_the_conversation)
    # A deadlock the append lost would otherwise be retried out of sight.
    monkeypatch.setattr(attempt_module, "retry_deadlock_victim", _first_try_only)
    cancel = append = None
    try:
        cancel = asyncio.create_task(store.attempt_commands.request_cancel(job_id=job["id"]))
        await asyncio.wait_for(held.wait(), timeout=10)
        append = asyncio.create_task(
            store.attempt_commands.append_event(
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                event_type="message",
                payload={"text": "Still reading the failing test."},
            )
        )
        await _blocked_by(pool, cancel_pid[0])
        release.set()
        assert await asyncio.wait_for(cancel, timeout=10) == "running"
        assert await asyncio.wait_for(append, timeout=10) is not None
    finally:
        release.set()
        await _settle(cancel)
        await _settle(append)


async def test_cancelling_a_first_turn_does_not_deadlock_with_its_reservation(
    pool, store: Storage, monkeypatch
):
    """A chat's first reservation takes the conversation before the turn, as a cancel does.

    Reserving a chat's first sandbox creates its group and membership, whose
    foreign keys share-lock the conversation, and the reservation already held
    the turn by then. A cancel takes the conversation and then the turn, so
    cancelling a first turn while its host reserved the sandbox deadlocked.
    """
    job = await _create_job(store, base_sha="a" * 40)
    claim = await _claim(
        store,
        worker_id="first-turn",
        lease_ttl_seconds=60,
        host=_HOST,
        host_credential_hash=_HOST_CREDENTIAL,
    )
    assert claim is not None and claim["id"] == job["id"]
    held, release = asyncio.Event(), asyncio.Event()
    cancel_pid: list[int] = []
    original = attempt_module.lock_conversation_by_job

    async def hold_the_conversation(conn, *, job_id):
        thread_id = await original(conn, job_id=job_id)
        cancel_pid.append(await conn.fetchval("SELECT pg_backend_pid()"))
        held.set()
        await release.wait()
        return thread_id

    monkeypatch.setattr(attempt_module, "lock_conversation_by_job", hold_the_conversation)
    cancel = reserve = None
    try:
        cancel = asyncio.create_task(store.attempt_commands.request_cancel(job_id=job["id"]))
        await asyncio.wait_for(held.wait(), timeout=10)
        reserve = asyncio.create_task(
            store.sandbox.reserve_thread_sandbox(
                thread_id=job["thread_id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                host=_HOST,
                sandbox_id="asbx_lock_order",
                container_name="freeinference-lock-order",
                runtime_id="claude-code",
                runtime_version=claim.get("runtime_version"),
                sandbox_image_digest=claim.get("sandbox_image_digest"),
                execution_profile=job["execution_profile"],
            )
        )
        await _blocked_by(pool, cancel_pid[0])
        release.set()
        assert await asyncio.wait_for(cancel, timeout=10) == "running"
        reserved = await asyncio.wait_for(reserve, timeout=10)
    finally:
        release.set()
        await _settle(cancel)
        await _settle(reserve)
    assert reserved is not None and reserved["sandbox_id"] == "asbx_lock_order"


async def test_abandoning_an_activation_takes_its_attempt_before_its_membership(
    pool, store: Storage
):
    """A lost activation's attempt is locked first, as the reaper locks it.

    Abandoning a lost activation wrote its membership and group and only then
    marked the attempt's context lost. The reaper takes an expired attempt and
    then its membership, as reservation and residency take a live one, so an
    expired runner abandoning its activation while the reaper reached it
    deadlocked with the reaper.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _expire_attempt(pool, store, claim["attempt_id"])
    abandon = None
    async with pool.acquire() as reaper:
        try:
            async with reaper.transaction():
                # The reaper's first lock: the expired attempt, with its job and
                # conversation, in ``_reap_attempt``'s one statement.
                await reaper.execute(
                    "SELECT a.id FROM agent_attempts a "
                    "JOIN agent_jobs j ON j.id = a.job_id "
                    "JOIN agent_threads t ON t.id = j.thread_id "
                    "WHERE a.id = $1 FOR UPDATE OF a, j, t",
                    claim["attempt_id"],
                )
                abandon = asyncio.create_task(
                    store.sandbox.abandon_thread_sandbox(
                        thread_id=job["thread_id"],
                        attempt_id=claim["attempt_id"],
                        lease_generation=claim["lease_generation"],
                        sandbox_generation=sandbox["generation"],
                        activation_generation=sandbox["activation_generation"],
                        lost=True,
                        detail="the runner lost its Agent CLI mid-turn",
                        idle_timeout_seconds=1800,
                    )
                )
                await _blocked_by(pool, await reaper.fetchval("SELECT pg_backend_pid()"))
                # Its next two, which the waiting abandon must not be holding.
                await reaper.execute(
                    "SELECT thread_id FROM agent_thread_sandbox_memberships "
                    "WHERE thread_id = $1 FOR UPDATE NOWAIT",
                    job["thread_id"],
                )
                await reaper.execute(
                    "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE NOWAIT",
                    sandbox["sandbox_id"],
                )
            abandoned = await asyncio.wait_for(abandon, timeout=10)
        finally:
            await _settle(abandon)
    assert abandoned is not None and abandoned["context_status"] == "lost"


async def test_losing_an_activation_waits_for_a_deletion_that_holds_its_queued_turns(
    pool, store: Storage
):
    """Both serialize on the conversation instead of crossing over its turns.

    Losing an activation fails the turns queued behind it, after writing its
    membership. Deleting the conversation cancels those turns and then
    releases that membership. Neither took the other's lock first, so a turn
    lost while its owner deleted the chat deadlocked: the owner's delete
    answered 500, or the runner's abandon failed. Losing it now takes the
    conversation after its attempt, where deletion starts.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    queued = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="and update the docs"
    )
    assert queued is not None and queued.get("error") is None
    abandon = None
    async with pool.acquire() as deletion:
        try:
            async with deletion.transaction():
                # ``delete_sessions`` up to its residency release: the
                # conversation, then the turns queued in it.
                await deletion.execute(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", job["thread_id"]
                )
                await deletion.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE "
                    "WHERE thread_id = $1 AND state = 'queued'",
                    job["thread_id"],
                )
                abandon = asyncio.create_task(
                    store.sandbox.abandon_thread_sandbox(
                        thread_id=job["thread_id"],
                        attempt_id=claim["attempt_id"],
                        lease_generation=claim["lease_generation"],
                        sandbox_generation=sandbox["generation"],
                        activation_generation=sandbox["activation_generation"],
                        lost=True,
                        detail="the runner lost its Agent CLI mid-turn",
                        idle_timeout_seconds=1800,
                    )
                )
                await _blocked_by(pool, await deletion.fetchval("SELECT pg_backend_pid()"))
                # Its next lock, which the waiting abandon must not be holding.
                await deletion.execute(
                    "SELECT thread_id FROM agent_thread_sandbox_memberships "
                    "WHERE thread_id = $1 FOR UPDATE NOWAIT",
                    job["thread_id"],
                )
            abandoned = await asyncio.wait_for(abandon, timeout=10)
        finally:
            await _settle(abandon)
    assert abandoned is not None and abandoned["context_status"] == "lost"


async def test_a_claim_takes_its_turns_group_before_the_host_row(pool, store: Storage):
    """A claim waiting for a group holds no host row that a finishing member needs.

    Finishing a turn writes its group and then gives its slot back, and a Fork
    sibling's next turn is claimed on the same host. The claim locked the host
    row before it chose the turn, then waited for the turn's group: a finish
    in between deadlocked with it, and when Postgres picked the finish, a turn
    that had settled was failed once its lease ran out.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, job, claim, sandbox)
    follow = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="and update the docs"
    )
    assert follow is not None and follow.get("error") is None
    claiming = None
    async with pool.acquire() as member:
        try:
            async with member.transaction():
                # A member's finish, up to its slot: it has written the group.
                await member.execute(
                    "UPDATE agent_sandboxes SET last_activity_at = NOW() WHERE id = $1",
                    sandbox["sandbox_id"],
                )
                claiming = asyncio.create_task(
                    _claim(
                        store,
                        worker_id="next-turn",
                        lease_ttl_seconds=60,
                        host=_HOST,
                        host_credential_hash=_HOST_CREDENTIAL,
                    )
                )
                await _blocked_by(pool, await member.fetchval("SELECT pg_backend_pid()"))
                # The finish's slot next, which the waiting claim must not hold.
                await member.execute(
                    "SELECT host FROM agent_runner_hosts WHERE host = $1 FOR UPDATE NOWAIT",
                    _HOST,
                )
            claimed = await asyncio.wait_for(claiming, timeout=10)
        finally:
            await _settle(claiming)
    assert claimed is not None and claimed["id"] == follow["id"]


async def test_a_claim_counts_an_environment_run_started_while_it_waited(
    pool, store: Storage, monkeypatch
):
    """Taken after the group, the host row is still locked before the slots are counted.

    An environment run takes a slot of its host without writing the host row:
    its claim locks the row, counts the slots in use and starts the run. A
    turn's claim counts those runs in its slot UPDATE as they were when that
    statement began, so it locks the row first, in a statement of its own.
    """
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, job, claim, sandbox)
    follow = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="and update the docs"
    )
    assert follow is not None and follow.get("error") is None
    environment = await _save_environment(store)
    run = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": environment["id"], "environment_id_provided": True},
        key="test",
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_runner_hosts SET slots_total = 1 WHERE host = $1", _HOST)
    claiming = None
    async with pool.acquire() as environment_claim:
        try:
            async with environment_claim.transaction():
                # ``claim_environment_run`` up to its commit: the host row, then the run.
                await environment_claim.execute(
                    "SELECT host FROM agent_runner_hosts WHERE host = $1 FOR UPDATE", _HOST
                )
                await environment_claim.execute(
                    "UPDATE agent_environment_runs SET state = 'running', host = $2, "
                    "lease_generation = lease_generation + 1, "
                    "lease_expires_at = NOW() + INTERVAL '120 seconds', started_at = NOW() "
                    "WHERE id = $1",
                    run["id"],
                    _HOST,
                )
                claiming = asyncio.create_task(
                    _claim(
                        store,
                        worker_id="next-turn",
                        lease_ttl_seconds=60,
                        host=_HOST,
                        host_credential_hash=_HOST_CREDENTIAL,
                    )
                )
                await _blocked_by(pool, await environment_claim.fetchval("SELECT pg_backend_pid()"))
            # The run holds the host's only slot.
            assert await asyncio.wait_for(claiming, timeout=10) is None
        finally:
            await _settle(claiming)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_runs SET cleanup_complete = TRUE WHERE id = $1", run["id"]
        )
    claimed = await _claim(
        store,
        worker_id="next-turn",
        lease_ttl_seconds=60,
        host=_HOST,
        host_credential_hash=_HOST_CREDENTIAL,
    )
    assert claimed is not None and claimed["id"] == follow["id"]


async def test_a_removal_counts_a_hosts_groups_under_their_locks(pool, store: Storage):
    """A workspace start fenced while a removal decides cannot strand its group.

    Revoking an idle host hands each stopped group its checkpoint covers to
    another host. A workspace request fences the group before it is
    forwarded, which ends that cover: the container may start. The removal
    counted the groups without locks, so a fence landing between the count
    and the hand-over took the group out of the hand-over, and the host was
    revoked with the group left on it, uncovered, its container about to
    start where nothing could report it.
    """
    _job, group = await _covered(pool, store)
    removing = None
    async with pool.acquire() as request:
        try:
            async with request.transaction():
                # The request's fence, held until it is forwarded to the host.
                fenced = await fence_sandbox_start(request, group["sandbox_id"])
                assert fenced is not None and fenced[0] == "stopped"
                removing = asyncio.create_task(store.host_pool.remove_host(host="runner-a"))
                await _blocked_by(pool, await request.fetchval("SELECT pg_backend_pid()"))
            removed = await asyncio.wait_for(removing, timeout=10)
        finally:
            await _settle(removing)
    # The fence came first, so the group no longer moves: the administrator is
    # asked, and the host is not revoked under it.
    assert removed == "cannot_move"
    assert (await _restore_state(pool, group["sandbox_id"]))["state"] == "stopped"
    kept = await store.host_pool.get_host(host="runner-a")
    assert kept is not None and kept["status"] == "online"


@pytest.mark.parametrize("sibling", ["claim", "finish"])
async def test_the_attempt_reaper_gives_a_slot_back_after_writing_the_group(
    pool, store: Storage, sibling: str
):
    """The reaper takes a group before its host, as a claim and a finish do.

    Every member of a group runs on the group's host. Claiming a member reads
    the group and then takes a slot; finishing one writes the group and then
    gives its slot back. The reaper gave an expired member's slot back before
    writing the group, so it deadlocked with a sibling claimed or finishing on
    the same host.
    """
    _job, claim, sandbox = await _reserve_shared_chat(store)
    await _expire_attempt(pool, store, claim["attempt_id"])
    reap = None
    async with pool.acquire() as member:
        try:
            async with member.transaction():
                if sibling == "claim":
                    await member.execute(
                        "SELECT id FROM agent_sandboxes WHERE id = $1 FOR SHARE",
                        sandbox["sandbox_id"],
                    )
                else:
                    await member.execute(
                        "UPDATE agent_sandboxes SET last_activity_at = NOW() WHERE id = $1",
                        sandbox["sandbox_id"],
                    )
                member_pid = await member.fetchval("SELECT pg_backend_pid()")
                reap = asyncio.create_task(store.attempt_commands.reap_expired())
                await _blocked_by(pool, member_pid)
                # The sibling's slot next, which the waiting reaper must not hold.
                await member.execute(
                    "SELECT host FROM agent_runner_hosts WHERE host = $1 FOR UPDATE NOWAIT",
                    _HOST,
                )
            actions = await asyncio.wait_for(reap, timeout=10)
        finally:
            await _settle(reap)
    assert [(action["attempt_id"], action["action"]) for action in actions] == [
        (claim["attempt_id"], "failed")
    ]


async def test_an_attempt_the_reaper_cannot_close_does_not_undo_the_others(
    pool, store: Storage, monkeypatch
):
    """Each expired attempt is reaped in a transaction of its own.

    The reaper closed every expired attempt in one transaction, so one the
    database refused -- a deadlock against a live writer is the usual way --
    took back every attempt the pass had already closed. It is left for the
    next pass now, and the others stay closed.
    """
    for n in range(3):
        await _create_job(store, task_prompt=f"task {n}")
    claims = []
    for n in range(3):
        claim = await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60)
        assert claim is not None
        claims.append(claim)
        await _expire_attempt(pool, store, claim["attempt_id"])
    stuck = claims[1]["attempt_id"]
    original = attempt_module.insert_event

    async def deadlock_on_one(conn, **fields):
        if fields["attempt_id"] == stuck:
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
        return await original(conn, **fields)

    monkeypatch.setattr(attempt_module, "insert_event", deadlock_on_one)
    actions = await store.attempt_commands.reap_expired()
    assert [action["attempt_id"] for action in actions] == [
        claims[0]["attempt_id"],
        claims[2]["attempt_id"],
    ]
    async with pool.acquire() as conn:
        statuses = await conn.fetch(
            "SELECT id, status FROM agent_attempts WHERE id = ANY($1::bigint[]) ORDER BY id",
            [claim["attempt_id"] for claim in claims],
        )
    assert [row["status"] for row in statuses] == ["superseded", "running", "superseded"]

    monkeypatch.undo()
    assert [action["attempt_id"] for action in await store.attempt_commands.reap_expired()] == [
        stuck
    ]


async def test_the_attempt_reaper_ends_a_turn_by_the_membership_it_wrote(pool, store: Storage):
    """A release between the reaper's read and its write leaves the context intact.

    The reaper reads an expired attempt's membership without locking it. A
    writer that released the activation before admission in between left the
    reaper's write matching nothing, and the reaper still marked the attempt's
    context lost, failed the turn queued behind it and failed its job as
    context lost, though the membership said the context was intact.
    """
    job, claim, _sandbox = await _reserve_shared_chat(store)
    queued = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="and update the docs"
    )
    assert queued is not None and queued.get("error") is None
    await _expire_attempt(pool, store, claim["attempt_id"])
    reap = None
    async with pool.acquire() as releaser:
        try:
            async with releaser.transaction():
                # The membership, as a release takes it: the reaper reads it
                # and then waits to write it.
                await releaser.execute(
                    "SELECT thread_id FROM agent_thread_sandbox_memberships "
                    "WHERE thread_id = $1 FOR UPDATE",
                    job["thread_id"],
                )
                reap = asyncio.create_task(store.attempt_commands.reap_expired(max_attempts=3))
                await _blocked_by(pool, await releaser.fetchval("SELECT pg_backend_pid()"))
                # What releasing the activation before admission leaves.
                await releaser.execute(
                    "UPDATE agent_thread_sandbox_memberships "
                    "SET active_attempt_id = NULL, resident_state = 'stopped' "
                    "WHERE thread_id = $1",
                    job["thread_id"],
                )
            actions = await asyncio.wait_for(reap, timeout=10)
        finally:
            await _settle(reap)
    # An ordinary expiry: the turn waits in the queue for its next attempt.
    assert actions == [{"job_id": job["id"], "attempt_id": claim["attempt_id"], "action": "queued"}]
    member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert member is not None and member["context_status"] == "new"
    still_queued = await store.conversation_queries.get_job(queued["id"])
    assert still_queued is not None and still_queued["state"] == "queued"
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT context_status FROM agent_attempts WHERE id = $1", claim["attempt_id"]
            )
            != "lost"
        )


async def test_releasing_an_activation_waits_for_the_reaper_holding_its_attempt(
    pool, store: Storage, monkeypatch
):
    """The reaper and a release cannot both decide what an activation left.

    Releasing an activation before admission took no lock on its attempt and
    asked it for nothing but its generation. It committed while the reaper
    held the expired attempt between reading the membership and writing it,
    so the membership said the context was intact while the reaper failed
    the job and the turn queued behind it as context lost. The release takes
    the attempt first now: it waits, and the reaper's answer stands alone.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    queued = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="and update the docs"
    )
    assert queued is not None and queued.get("error") is None
    await _expire_attempt(pool, store, claim["attempt_id"])
    original = attempt_module.insert_event
    reaper_pid: list[int] = []
    resume = asyncio.Event()

    async def pause_after_the_read(conn, **fields):
        # The reaper's control event: after it read the membership, before it
        # writes the membership.
        if fields["attempt_id"] == claim["attempt_id"]:
            reaper_pid.append(await conn.fetchval("SELECT pg_backend_pid()"))
            await resume.wait()
        return await original(conn, **fields)

    monkeypatch.setattr(attempt_module, "insert_event", pause_after_the_read)
    reap = asyncio.create_task(store.attempt_commands.reap_expired(max_attempts=3))
    abandon = None
    try:
        async with asyncio.timeout(10):
            while not reaper_pid:
                await asyncio.sleep(0.01)
        abandon = asyncio.create_task(
            store.sandbox.abandon_thread_sandbox(
                thread_id=job["thread_id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                sandbox_generation=sandbox["generation"],
                activation_generation=sandbox["activation_generation"],
                lost=False,
                detail="checkout failed",
                idle_timeout_seconds=1800,
            )
        )
        await _blocked_by(pool, reaper_pid[0])
        resume.set()
        actions = await asyncio.wait_for(reap, timeout=10)
        abandoned = await asyncio.wait_for(abandon, timeout=10)
    finally:
        resume.set()
        await _settle(abandon)
        await _settle(reap)
    assert abandoned is None
    assert [(action["attempt_id"], action["action"]) for action in actions] == [
        (claim["attempt_id"], "failed")
    ]
    member = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert member is not None and member["context_status"] == "lost"
    failed = await store.conversation_queries.get_job(queued["id"])
    assert failed is not None and failed["state"] == "failed"


async def test_deleting_snapshots_does_not_deadlock_with_a_build_receipt(
    pool, store: Storage, monkeypatch
):
    """A build's receipt and a deletion of its snapshots both take the owner's snapshot lock first.

    A receipt locks the snapshot it makes ready and that build's run, then
    the older ready build of the same installation it supersedes. Deleting an
    environment's snapshots locked all of them in the order its one statement
    scanned them, and then their runs. On a sequential scan -- the plan a small
    snapshot table gets -- it took the older build first and then waited for
    the one the receipt held, while the receipt waited for the older build. An
    owner who deleted or rebuilt snapshots while a prebuild reported got a 500,
    or the prebuild failed and left its uploaded object unrecorded.
    """
    older, binding, _older_run = await _ready_snapshot(store, pool, monkeypatch)
    snapshots = store.environment_snapshots
    newer = await snapshots.queue_prebuild(
        user_id="alice", binding_id=binding, repo="org/repo", base_sha="f" * 40, requested=True
    )
    build = await _claim_environment_run(store, capable=True)
    assert build is not None and build["snapshot_id"] == newer
    assert await snapshots.capture_target(build, "environment-host")
    environment_id = (await store.environments.list_environments("alice"))[0]["id"]
    async with pool.acquire() as conn:
        # Statistics that say the table is small, as production's are before it
        # grows: the deletion then scans the rows in their order, older first.
        await conn.execute("ANALYZE agent_environment_snapshots")
    receipt = deletion = None
    async with pool.acquire() as rival:
        try:
            async with rival.transaction():
                # Hold the build's run, so the receipt stops after taking the
                # rows it locks before the run.
                await rival.execute(
                    "SELECT id FROM agent_environment_runs WHERE id = $1 FOR KEY SHARE",
                    build["id"],
                )
                receipt = asyncio.create_task(
                    snapshots.record_capture(
                        run_id=build["id"],
                        host="environment-host",
                        generation=build["lease_generation"],
                        receipt=_receipt(newer, source_sha="f" * 40),
                    )
                )
                receipt_pid = await _waiting_on(
                    pool, await rival.fetchval("SELECT pg_backend_pid()")
                )
                deletion = asyncio.create_task(snapshots.discard("alice", environment_id))
                await _blocked_by(pool, receipt_pid)
            # The run is free: the receipt goes on to the older build.
            outcomes = await asyncio.wait_for(
                asyncio.gather(receipt, deletion, return_exceptions=True), timeout=20
            )
        finally:
            await _settle(receipt)
            await _settle(deletion)
    assert outcomes == [True, 2]
    async with pool.acquire() as conn:
        states = dict(
            await conn.fetch(
                "SELECT id, state FROM agent_environment_snapshots WHERE id = ANY($1::text[])",
                [older, newer],
            )
        )
    assert states == {older: "deleting", newer: "deleting"}


_LOCK_MEMBER = (
    "SELECT thread_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1 FOR UPDATE"
)


async def _forked(store: Storage, monkeypatch, *, target_sorts_first: bool) -> tuple[dict, dict]:
    """A Fork of a settled chat, its coordinator claimed and both residencies established.

    A new chat's id is random, so a Fork's target sorts before its source half
    the time; this one's sorts first, or last, as asked.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, job, claim, sandbox)
    target = "athr_" + ("0" if target_sorts_first else "f") * 16
    monkeypatch.setattr(session_fork_module, "new_thread_id", lambda: target)
    operation = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=job["user_id"], idempotency_key="lock-order-fork"
    )
    assert operation is not None and operation.get("error") is None, operation
    assert (operation["target_thread_id"] < job["thread_id"]) is target_sorts_first
    assert await store.session_fork.claim_session_fork_materialization(
        operation_id=operation["id"], coordinator_token="coordinator", lease_seconds=990
    )
    residencies = await store.session_fork.establish_session_fork_residencies(
        operation_id=operation["id"], coordinator_token="coordinator", ttl_seconds=120
    )
    assert residencies is not None
    return {**operation, "residencies": residencies}, job


async def _materialized(store: Storage, operation: dict) -> None:
    """Record the host's receipt for the Fork, as its coordinator does."""
    residencies = operation["residencies"]
    receipt = {
        key: operation[key]
        for key in (
            "sandbox_id",
            "sandbox_generation",
            "container_id",
            "runtime_id",
            "runtime_version",
            "source_thread_id",
            "source_native_session_id",
            "target_thread_id",
            "target_resident_slot_id",
        )
    }
    recorded = await store.session_fork.record_session_fork_materialized(
        operation_id=operation["id"],
        coordinator_token="coordinator",
        receipt={
            **receipt,
            "operation_id": operation["id"],
            "target_native_session_id": "native-child",
            "source_residency_generation": residencies["source"]["generation"],
            "target_residency_generation": residencies["target"]["generation"],
            "source_process_generation": 2,
            "target_process_generation": 1,
        },
    )
    assert recorded is not None and recorded["state"] == "materialized"


@pytest.mark.parametrize("target_sorts_first", [True, False], ids=["target-first", "source-first"])
@pytest.mark.parametrize("writer", ["establish", "finalize", "reap", "delete"])
async def test_a_write_to_both_fork_members_takes_them_in_a_heartbeats_order(
    pool, store: Storage, monkeypatch, writer: str, target_sorts_first: bool
):
    """A write to both members of a Fork takes them in thread order, as a heartbeat does.

    A heartbeat extending both residencies locks the two members in thread
    order. Fork's writers took the source's and then the target's, and the
    target's random id sorts first half the time: a beat holding the target
    then waited for the source while the Fork held the source and waited for
    the target. Owner deletion of both chats updated their members in
    whatever order its plan met them. The heartbeat retries as a deadlock's
    victim; the owner's Fork and deletion do not, and failed with a 500. The
    rival here is the beat part-way through its lock: it holds the member
    first in thread order and then asks for the other one, which the writer
    must not be holding while it waits.
    """
    operation, job = await _forked(store, monkeypatch, target_sorts_first=target_sorts_first)
    if writer in {"finalize", "delete"}:
        await _materialized(store, operation)
    if writer == "delete":
        assert await store.session_fork.finalize_session_fork(operation_id=operation["id"])
    if writer == "reap":
        # Its coordinator stopped before the host's receipt was recorded.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_session_forks "
                "SET coordinator_lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
                operation["id"],
            )
    members = (job["thread_id"], operation["target_thread_id"])
    writes = {
        "establish": lambda: store.session_fork.establish_session_fork_residencies(
            operation_id=operation["id"], coordinator_token="coordinator", ttl_seconds=120
        ),
        "finalize": lambda: store.session_fork.finalize_session_fork(operation_id=operation["id"]),
        "reap": lambda: store.session_fork.reap_stalled_session_forks(stall_seconds=0),
        "delete": lambda: store.conversation_commands.delete_sessions(
            thread_ids=list(members), user_id=job["user_id"]
        ),
    }
    first, second = sorted(members)
    write = None
    async with pool.acquire() as beat:
        try:
            async with beat.transaction():
                await beat.execute(_LOCK_MEMBER, first)
                write = asyncio.create_task(writes[writer]())
                await _blocked_by(pool, await beat.fetchval("SELECT pg_backend_pid()"))
                await _close_a_cycle(beat, _LOCK_MEMBER, second)
            written = await asyncio.wait_for(write, timeout=10)
        finally:
            await _settle(write)
    assert written
    if writer == "delete":
        assert sorted(written["thread_ids"]) == sorted(members)
        assert len(written["released_residencies"]) == 2


_LOCK_CONVERSATION = "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE"


@pytest.mark.parametrize("target_sorts_first", [True, False], ids=["target-first", "source-first"])
async def test_fork_finalization_takes_both_conversations_in_id_order(
    pool, store: Storage, monkeypatch, target_sorts_first: bool
):
    """Finalizing a Fork takes its two conversations in id order, as an archive does.

    An owner archiving or deleting several chats locks their conversations in
    id order. Finalization locked the source's and then the target's, and the
    target's random id sorts first half the time: an archive holding the
    target then waited for the source while finalization held the source and
    waited for the target, and Postgres aborted one of them. The rival here is
    that archive part-way through its lock: it holds the conversation first
    in id order and then asks for the other one, which finalization must not
    be holding while it waits.
    """
    operation, job = await _forked(store, monkeypatch, target_sorts_first=target_sorts_first)
    await _materialized(store, operation)
    first, second = sorted((job["thread_id"], operation["target_thread_id"]))
    finalize = None
    async with pool.acquire() as owner:
        try:
            async with owner.transaction():
                await owner.execute(_LOCK_CONVERSATION, first)
                finalize = asyncio.create_task(
                    store.session_fork.finalize_session_fork(operation_id=operation["id"])
                )
                await _blocked_by(pool, await owner.fetchval("SELECT pg_backend_pid()"))
                await _close_a_cycle(owner, _LOCK_CONVERSATION, second)
            anchor = await asyncio.wait_for(finalize, timeout=10)
        finally:
            await _settle(finalize)
    assert anchor is not None and anchor["thread_id"] == operation["target_thread_id"]


@pytest.mark.parametrize("in_fork_order", [False, True], ids=["reversed", "in-order"])
async def test_the_fork_reaper_takes_the_targets_it_fails_in_id_order(
    pool, store: Storage, monkeypatch, in_fork_order: bool
):
    """A reaper pass that fails several Forks takes their targets in id order.

    A Fork left preparing with no live coordinator is failed, and its hidden
    target tombstoned. The reaper tombstoned each target as it reached its
    Fork, in Fork id order, which the targets' random ids need not share:
    holding one target, it waited for the other while an owner archiving or
    deleting both held that one and waited for the first. The rival here is
    that archive part-way through its lock.
    """
    # A finalized Fork is a chat of its own, which can be forked in turn.
    operation, job = await _forked(store, monkeypatch, target_sorts_first=False)
    await _materialized(store, operation)
    child = await store.session_fork.finalize_session_fork(operation_id=operation["id"])
    assert child is not None
    # The reaper meets the Fork of the origin first. Its target sorts first,
    # as the Forks do, or last.
    targets = ["athr_" + "1" * 16, "athr_" + "2" * 16]
    fork_ids = iter(["asfork_" + "1" * 32, "asfork_" + "2" * 32])
    target_ids = iter(targets if in_fork_order else reversed(targets))
    monkeypatch.setattr(session_fork_module, "new_session_fork_id", lambda: next(fork_ids))
    monkeypatch.setattr(session_fork_module, "new_thread_id", lambda: next(target_ids))
    stalled = []
    for source in (job, child):
        fork = await store.session_fork.prepare_session_fork(
            source_job_id=source["id"], user_id=job["user_id"], idempotency_key=source["id"]
        )
        assert fork is not None and fork.get("error") is None, fork
        stalled.append(fork)
    first, second = sorted(fork["target_thread_id"] for fork in stalled)
    reap = None
    async with pool.acquire() as owner:
        try:
            async with owner.transaction():
                await owner.execute(_LOCK_CONVERSATION, first)
                reap = asyncio.create_task(
                    store.session_fork.reap_stalled_session_forks(stall_seconds=0)
                )
                await _blocked_by(pool, await owner.fetchval("SELECT pg_backend_pid()"))
                await _close_a_cycle(owner, _LOCK_CONVERSATION, second)
            reaped = await asyncio.wait_for(reap, timeout=10)
        finally:
            await _settle(reap)
    assert [(action["id"], action["recovery_action"]) for action in reaped] == [
        (fork["id"], "cleanup") for fork in stalled
    ]
