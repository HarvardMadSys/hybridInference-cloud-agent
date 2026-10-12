"""Writers that meet on the same rows, interleaved on real Postgres.

The order every transaction takes shared rows in is written down once, in
:mod:`cloud_agent.storage`. A test here holds one writer part-way through -- on
a connection of its own, taking the rows in the order that writer's code does,
or by pausing the real call -- runs another real repository call into it, and
then lets the held writer go on: two orders over the same rows is a deadlock,
and Postgres says so. The capture and attempt reapers are here too, for what a
writer that ran in between leaves them to find.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio
import hashlib
from typing import TYPE_CHECKING

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _expire_attempt,
    _reserve_retained_chat,
    _reserve_shared_chat,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)

from cloud_agent.storage.repositories import (
    attempt_commands as attempt_module,
    snapshot as snapshot_module,
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
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
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


async def _run_next_turn(store: Storage, parent: dict, sandbox: dict) -> dict:
    """Send a follow-up into the same workspace and run it to the end."""
    follow = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"], user_id=parent["user_id"], prompt="and update the docs"
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
    async with asyncio.timeout(10), pool.acquire() as observer:
        while not await observer.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks "
            "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
            pid,
        ):
            await asyncio.sleep(0.01)


async def _settle(task: asyncio.Task | None) -> None:
    """Do not leave a call behind when an assertion ends the test early."""
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _snapshots(pool, sandbox_id: str) -> list[dict]:
    """Every snapshot of a group, oldest first, with the turns it includes."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.state, s.workspace_epoch, s.anchor_job_id,
                   ARRAY(
                       SELECT included.job_id FROM agent_snapshot_jobs included
                       WHERE included.snapshot_id = s.id
                       ORDER BY included.created_at, included.job_id
                   ) AS included
            FROM agent_sandbox_snapshots s
            WHERE s.sandbox_id = $1
            ORDER BY s.id
            """,
            sandbox_id,
        )
    return [dict(row) for row in rows]


@pytest.mark.parametrize("recovery", ["deferred", "reaped"])
async def test_a_capture_handed_back_folds_into_the_snapshot_a_turn_settled_meanwhile(
    pool, store: Storage, recovery: str
):
    """A capture handed back joins the pending snapshot of a turn settled while it was out.

    A turn is not refused while its workspace is captured, so the next turn's
    settlement opens a second, pending snapshot behind the capturing one. The
    capture handed back by a busy host, or by the stall reaper, used to become
    pending beside it, which the one-pending-snapshot index refuses. The claim
    then stayed ``capturing`` and its group ``snapshotting`` for good, and the
    reaper's single statement failed for every workspace in the deployment.
    """
    first, first_claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, first, first_claim, sandbox)
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["anchor_job_id"] == first["id"]

    second = await _run_next_turn(store, first, sandbox)
    opened = [
        row
        for row in await _snapshots(pool, sandbox["sandbox_id"])
        if row["id"] != capture["snapshot_id"]
    ]
    assert [(row["state"], row["included"]) for row in opened] == [("pending", [second["id"]])]

    if recovery == "deferred":
        assert await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            detail="the host is busy",
        )
    else:
        assert await store.snapshot.reap_stalled_sandbox_snapshot_captures(stall_seconds=0) == [
            capture["snapshot_id"]
        ]

    # One pending snapshot: the second turn's, at its boundary, carrying both
    # turns. The capture's own row is gone, so its receipt is a stale one.
    assert await _snapshots(pool, sandbox["sandbox_id"]) == [
        {
            "id": opened[0]["id"],
            "state": "pending",
            "workspace_epoch": opened[0]["workspace_epoch"],
            "anchor_job_id": second["id"],
            "included": [first["id"], second["id"]],
        }
    ]
    late = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=late,
            content_sha256=hashlib.sha256(late.encode()).hexdigest(),
        )
        is None
    )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT snapshot_state FROM agent_sandboxes WHERE id = $1", sandbox["sandbox_id"]
            )
            == "pending"
        )
        if recovery == "deferred":
            assert await store.snapshot.claim_due_sandbox_snapshot() is None, "not before its retry"
            await conn.execute(
                "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1",
                opened[0]["id"],
            )

    again = await store.snapshot.claim_due_sandbox_snapshot()
    assert again is not None
    assert again["snapshot_id"] == opened[0]["id"]
    assert again["anchor_job_id"] == second["id"]
    patch = "diff --git a/docs.md b/docs.md\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=again["snapshot_id"],
            sandbox_id=again["sandbox_id"],
            sandbox_generation=again["generation"],
            container_id=again["container_id"],
            workspace_epoch=again["workspace_epoch"],
            base_sha=again["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    assert publication["included_job_ids"] == [first["id"], second["id"]]


@pytest.mark.parametrize("recovery", ["deferred", "reaped"])
async def test_a_capture_handed_back_does_not_wait_for_a_turn_a_follow_up_holds(
    pool, store: Storage, recovery: str
):
    """Folding a capture into the pending snapshot locks none of their turns.

    A follow-up takes the conversation's newest turn and then the group. A
    capture is handed back with its group locked, so a fold that locked a turn
    it moved -- inserting a turn's row into another snapshot, or naming a new
    anchor, share-locks that turn through its foreign key -- waited for the
    follow-up while holding the group the follow-up waits for next.
    """
    first, first_claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, first, first_claim, sandbox)
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    second = await _run_next_turn(store, first, sandbox)
    recover = None
    async with pool.acquire() as follow_up:
        try:
            async with follow_up.transaction():
                # A follow-up's first two locks, in ``insert_follow_up``'s order:
                # the newest turn is the one the settled pending snapshot holds.
                await follow_up.execute(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", first["thread_id"]
                )
                newest = await follow_up.fetchval(
                    "SELECT id FROM agent_jobs WHERE thread_id = $1 "
                    "ORDER BY turn_no DESC LIMIT 1 FOR UPDATE",
                    first["thread_id"],
                )
                assert newest == second["id"]
                if recovery == "deferred":
                    recover = asyncio.create_task(
                        store.snapshot.defer_sandbox_snapshot_capture(
                            snapshot_id=capture["snapshot_id"],
                            sandbox_id=capture["sandbox_id"],
                            sandbox_generation=capture["generation"],
                            container_id=capture["container_id"],
                            detail="the host is busy",
                        )
                    )
                else:
                    recover = asyncio.create_task(
                        store.snapshot.reap_stalled_sandbox_snapshot_captures(stall_seconds=0)
                    )
                # Handed back while the follow-up holds the turn...
                assert await asyncio.wait_for(recover, timeout=10)
                # ...so the follow-up's next two locks are free.
                await follow_up.execute(
                    "SELECT thread_id FROM agent_thread_sandbox_memberships "
                    "WHERE thread_id = $1 FOR UPDATE",
                    first["thread_id"],
                )
                await follow_up.execute(
                    "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE NOWAIT",
                    sandbox["sandbox_id"],
                )
        finally:
            await _settle(recover)
    assert [
        (row["state"], row["included"]) for row in await _snapshots(pool, sandbox["sandbox_id"])
    ] == [("pending", [first["id"], second["id"]])]


async def test_a_stalled_capture_the_reaper_cannot_hand_back_does_not_hold_back_the_others(
    pool, store: Storage, monkeypatch
):
    """Each stalled capture is handed back in a transaction of its own.

    The reaper handed back every stalled capture of the deployment in one
    statement, so a capture the database refused kept every workspace's
    capture stuck with it. That one is left for the next pass now, and the
    others go back to the queue.
    """
    first, first_claim, first_sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, first, first_claim, first_sandbox)
    other, other_claim, other_sandbox = await _reserve_retained_chat(store)
    await _run_turn(store, other, other_claim, other_sandbox, container_id="container-lock-other")
    captures = [await store.snapshot.claim_due_sandbox_snapshot() for _ in range(2)]
    assert [capture["sandbox_id"] for capture in captures if capture is not None] == [
        first_sandbox["sandbox_id"],
        other_sandbox["sandbox_id"],
    ]
    stuck, freed = (int(capture["snapshot_id"]) for capture in captures if capture is not None)
    original = snapshot_module._fold_into_pending_snapshot

    async def refuse_one(conn, *, snapshot_id, sandbox_id):
        if snapshot_id == stuck:
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
        return await original(conn, snapshot_id=snapshot_id, sandbox_id=sandbox_id)

    monkeypatch.setattr(snapshot_module, "_fold_into_pending_snapshot", refuse_one)
    assert await store.snapshot.reap_stalled_sandbox_snapshot_captures(stall_seconds=0) == [freed]
    async with pool.acquire() as conn:
        states = await conn.fetch(
            "SELECT id, state FROM agent_sandbox_snapshots WHERE id = ANY($1::bigint[]) "
            "ORDER BY id",
            [stuck, freed],
        )
    assert [(row["id"], row["state"]) for row in states] == [
        (stuck, "capturing"),
        (freed, "pending"),
    ]

    monkeypatch.undo()
    assert await store.snapshot.reap_stalled_sandbox_snapshot_captures(stall_seconds=0) == [stuck]


async def _claimed_for_publication(store: Storage) -> tuple[dict, dict, dict]:
    """A finished retained turn whose captured snapshot a publisher has claimed."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, job, claim, sandbox)
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None and publication["included_job_ids"] == [job["id"]]
    return job, sandbox, publication


async def test_a_publication_record_takes_its_turns_before_their_group(pool, store: Storage):
    """Recording a publication waits behind a follow-up instead of crossing it.

    A follow-up, a settlement and a Fork take the conversation's newest turn,
    then its membership, then the group. Recording a publication wrote the
    group first and the included turns after it, so a message sent while a
    chat's snapshot was being recorded deadlocked with it -- and a record that
    lost left the pull request it was recording open and unrecorded.
    """
    job, sandbox, publication = await _claimed_for_publication(store)
    pr_url = "https://github.com/example-org/example-repo/pull/1"
    publish = None
    async with pool.acquire() as follow_up:
        try:
            async with follow_up.transaction():
                # A follow-up's first two locks, in ``insert_follow_up``'s order.
                await follow_up.execute(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", job["thread_id"]
                )
                await follow_up.execute(
                    "SELECT id FROM agent_jobs WHERE thread_id = $1 "
                    "ORDER BY turn_no DESC LIMIT 1 FOR UPDATE",
                    job["thread_id"],
                )
                follow_up_pid = await follow_up.fetchval("SELECT pg_backend_pid()")
                publish = asyncio.create_task(
                    store.snapshot.record_snapshot_publish(
                        snapshot_id=publication["snapshot_id"],
                        pr_url=pr_url,
                        commit_sha="b" * 40,
                    )
                )
                await _blocked_by(pool, follow_up_pid)
                # Its next two, which the waiting record must not be holding.
                await follow_up.execute(
                    "SELECT thread_id FROM agent_thread_sandbox_memberships "
                    "WHERE thread_id = $1 FOR UPDATE",
                    job["thread_id"],
                )
                await follow_up.execute(
                    "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE NOWAIT",
                    sandbox["sandbox_id"],
                )
            assert await asyncio.wait_for(publish, timeout=10)
        finally:
            await _settle(publish)
    published = await store.conversation_queries.get_job(job["id"])
    assert published is not None and published["published_pr_url"] == pr_url


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


async def test_titling_a_thread_does_not_deadlock_with_its_streaming_reply(
    pool, store: Storage, monkeypatch
):
    """Title generation takes the attempt before the conversation, as an append does.

    It took the conversation first and then checked the attempt it was asked
    for under a share lock, the reverse of every reply its turn appends.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["title_generation_pending"]
    held, release = asyncio.Event(), asyncio.Event()
    append_pid: list[int] = []
    original = attempt_module.insert_event

    async def hold_the_attempt(conn, **fields):
        if fields["event_type"] == "message":
            append_pid.append(await conn.fetchval("SELECT pg_backend_pid()"))
            held.set()
            await release.wait()
        return await original(conn, **fields)

    monkeypatch.setattr(attempt_module, "insert_event", hold_the_attempt)
    append = title = None
    try:
        append = asyncio.create_task(
            store.attempt_commands.append_event(
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                event_type="message",
                payload={"text": "Reading the failing test."},
            )
        )
        await asyncio.wait_for(held.wait(), timeout=10)
        title = asyncio.create_task(
            store.conversation_commands.set_generated_thread_title(
                job_id=job["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                title="Flaky test fix",
            )
        )
        await _blocked_by(pool, append_pid[0])
        release.set()
        assert await asyncio.wait_for(append, timeout=10) is not None
        assert await asyncio.wait_for(title, timeout=10)
    finally:
        release.set()
        await _settle(append)
        await _settle(title)
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == "Flaky test fix"


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
