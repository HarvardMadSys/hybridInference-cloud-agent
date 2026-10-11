"""Writers that meet on the same rows, interleaved on real Postgres.

The order every transaction takes shared rows in is written down once, in
:mod:`cloud_agent.storage`. A test here holds one writer's locks open on a
connection of its own, in the order that writer's code takes them, runs the
real repository call into it, and then asks for the rows the held writer takes
next: two orders over the same rows is a deadlock, and Postgres says so.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_shared_chat,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)

from cloud_agent.storage.repositories import attempt_commands as attempt_module

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_HOST = "shared-runner"
_HOST_CREDENTIAL = "test-credential-hash"


async def _run_turn(
    store: Storage, job: dict, claim: dict, sandbox: dict, *, context_status: str = "new"
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
        **fence, container_id="container-lock-order", context_status=context_status
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
async def test_a_turn_settled_during_a_capture_is_folded_into_the_capture_handed_back(
    pool, store: Storage, recovery: str
):
    """A capture handed back carries the turn that settled while it was out.

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

    # One pending snapshot: the claim that was handed back, now at the second
    # turn's boundary and carrying both turns.
    assert await _snapshots(pool, sandbox["sandbox_id"]) == [
        {
            "id": capture["snapshot_id"],
            "state": "pending",
            "workspace_epoch": opened[0]["workspace_epoch"],
            "anchor_job_id": second["id"],
            "included": [first["id"], second["id"]],
        }
    ]
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
                capture["snapshot_id"],
            )

    again = await store.snapshot.claim_due_sandbox_snapshot()
    assert again is not None
    assert again["snapshot_id"] == capture["snapshot_id"]
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
