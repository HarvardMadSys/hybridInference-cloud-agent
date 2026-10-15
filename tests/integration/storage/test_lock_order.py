"""Writers that meet on the same rows, interleaved on real Postgres.

The order every transaction takes shared rows in is written down once, in
:mod:`cloud_agent.storage`. A test here holds one writer part-way through -- on
a connection of its own, taking the rows in the order that writer's code does,
or by pausing the real call -- runs another real repository call into it, and
then lets the held writer go on: two orders over the same rows is a deadlock,
and Postgres says so. The capture and attempt reapers are here too, for what a
writer that ran in between leaves them to find, and so is the event append that
is tried again when Postgres picks it as a deadlock's victim: a rival closes a
real cycle against it.
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


async def _first_try_only(write: str, transaction):
    """Run a write once: the lock order alone has to keep it out of a cycle."""
    return await transaction()


async def _request(store: Storage, job: dict) -> int:
    """The owner's publication request: a turn boundary publishes nothing."""
    requested = await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=job["user_id"]
    )
    assert requested.get("requested") is True, requested
    return int(requested["snapshot_id"])


async def _legacy_turn_snapshot(pool, sandbox_id: str, job: dict) -> None:
    """The pending snapshot turn settlement opened before publication was a request.

    Settlement used to open one for each turn, even while an earlier snapshot of
    the group was being captured. A deployment upgraded during such a capture
    still holds it, and a capture handed back must fold into it.
    """
    async with pool.acquire() as conn, conn.transaction():
        snapshot_id = await conn.fetchval(
            """
            INSERT INTO agent_sandbox_snapshots (sandbox_id, workspace_epoch, anchor_job_id, base_sha)
            SELECT id, workspace_epoch, $2, published_base_sha FROM agent_sandboxes WHERE id = $1
            RETURNING id
            """,
            sandbox_id,
            job["id"],
        )
        await conn.execute(
            "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, $2)",
            snapshot_id,
            job["id"],
        )


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

    A turn is not refused while its workspace is captured, and settlement used
    to open a second, pending snapshot behind the capturing one; a deployment
    upgraded mid-capture still holds one. The capture handed back by a busy
    host, or by the stall reaper, used to become pending beside it, which the
    one-pending-snapshot index refuses. The claim then stayed ``capturing`` and
    its group ``snapshotting`` for good, and the reaper's single statement
    failed for every workspace in the deployment.
    """
    first, first_claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, first, first_claim, sandbox)
    await _request(store, first)
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["anchor_job_id"] == first["id"]

    second = await _run_next_turn(store, first, sandbox)
    await _legacy_turn_snapshot(pool, sandbox["sandbox_id"], second)
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
    await _request(store, first)
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    second = await _run_next_turn(store, first, sandbox)
    await _legacy_turn_snapshot(pool, sandbox["sandbox_id"], second)
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
    await _request(store, first)
    other, other_claim, other_sandbox = await _reserve_retained_chat(store)
    await _run_turn(store, other, other_claim, other_sandbox, container_id="container-lock-other")
    await _request(store, other)
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
    await _request(store, job)
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
                        expected_publish_attempt_count=publication["publish_attempt_count"],
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


async def test_an_idle_only_message_refuses_while_a_publication_holds_its_newest_turn(
    pool, store: Storage
):
    """An idle-only message does not wait under the group for a turn a publication holds.

    A publication records its included turns before their group, and the
    idle-only submit locks the group before the newest turn. That is safe only
    because the submit locks the turn once the group's newest snapshot reads
    published, which a publication still in flight has not committed.
    """
    job, sandbox, publication = await _claimed_for_publication(store)
    async with pool.acquire() as publisher, publisher.transaction():
        # ``record_snapshot_publish`` up to its group: the snapshot, then its turns.
        await publisher.execute(
            "UPDATE agent_sandbox_snapshots SET state = 'published', published_pr_url = $2, "
            "published_commit_sha = $3, published_at = NOW() WHERE id = $1",
            publication["snapshot_id"],
            "https://github.com/example-org/example-repo/pull/3",
            "d" * 40,
        )
        await publisher.fetch(
            "SELECT id FROM agent_jobs WHERE id IN ("
            "SELECT job_id FROM agent_snapshot_jobs WHERE snapshot_id = $1) "
            "ORDER BY id FOR NO KEY UPDATE",
            publication["snapshot_id"],
        )
        refused = await asyncio.wait_for(
            store.pending_messages.submit_message(
                thread_id=job["thread_id"],
                user_id=job["user_id"],
                prompt="Watch the PR and merge",
                skill_ids=None,
                attachment_ids=None,
                model=None,
                reasoning_effort=None,
                require_idle=True,
                expected_latest_job_id=job["id"],
            ),
            timeout=10,
        )
        assert refused == {"error": "conversation_changed"}
        # The submit gave the group back, so the publication goes on.
        await publisher.execute(
            "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE NOWAIT",
            sandbox["sandbox_id"],
        )


async def test_a_publication_request_does_not_wait_under_the_group_for_a_held_turn(
    pool, store: Storage
):
    """A request share-locks its turns after their group, so it never waits for them.

    A follow-up holds its conversation's newest turn while it waits for the
    group. A request that waited for that turn while holding the group would
    close the cycle; it gives the group back and says the workspace is busy.
    """
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, job, claim, sandbox)
    async with pool.acquire() as holder, holder.transaction():
        await holder.execute("SELECT id FROM agent_jobs WHERE id = $1 FOR UPDATE", job["id"])
        refused = await asyncio.wait_for(
            store.snapshot.request_snapshot_publication(
                thread_id=job["thread_id"], user_id=job["user_id"]
            ),
            timeout=10,
        )
        assert refused == {"error": "workspace_busy"}
        # The request gave the group back, and opened nothing.
        await holder.execute(
            "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE NOWAIT",
            sandbox["sandbox_id"],
        )
    assert await _snapshots(pool, sandbox["sandbox_id"]) == []
    assert await _request(store, job)


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
    # A deadlock the append lost would otherwise be retried out of sight.
    monkeypatch.setattr(attempt_module, "retry_deadlock_victim", _first_try_only)
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
    queued = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="and update the docs"
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
