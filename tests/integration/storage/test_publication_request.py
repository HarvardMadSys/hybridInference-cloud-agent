"""The owner's publication request, on real Postgres.

The platform never publishes on its own: a turn boundary opens no snapshot, and
leaves its workspace free to stop and its chat free to delete. The owner's
request opens the workspace's next snapshot, holding every turn settled since
the last one, and the capture, publication and base import that always
followed a snapshot follow it. A request made while one is on its way returns
that one.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    _reserve_shared_chat,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_lock_order import _run_turn

from cloud_agent.storage.repositories.sandbox import _GROUP_WORK_PENDING_SQL
from cloud_agent_shared.git.temporary_workspace import TEMPORARY_WORKSPACE_BASE_SHA

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_PATCH = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"


async def _request(store, job: dict, *, user_id: str | None = None) -> dict:
    return await store.snapshot.request_snapshot_publication(
        thread_id=job["thread_id"], user_id=user_id or job["user_id"]
    )


async def _ended_turn(store) -> tuple[dict, dict]:
    job, claim, sandbox = await _reserve_shared_chat(store)
    await _run_turn(store, job, claim, sandbox)
    return job, sandbox


async def _group(pool, sandbox_id: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT workspace_epoch, snapshot_state FROM agent_sandboxes WHERE id = $1",
            sandbox_id,
        )
    assert row is not None
    return dict(row)


async def test_a_turn_boundary_publishes_nothing_and_holds_nothing_back(store, pool):
    """No snapshot, no pending group: the workspace can stop and its chat be deleted."""
    job, sandbox = await _ended_turn(store)

    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_sandbox_snapshots WHERE sandbox_id = $1",
                sandbox["sandbox_id"],
            )
            == 0
        )
        assert await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox["sandbox_id"]) is False
    assert (await _group(pool, sandbox["sandbox_id"]))["snapshot_state"] == "quiescent"
    assert await store.snapshot.claim_due_sandbox_snapshot() is None
    view = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert view is not None
    assert view["workspace_publication_state"] is None
    assert view["workspace_unpublished_changes"] is True
    assert view["workspace_pr_url"] is None
    sessions = await store.conversation_queries.list_sessions(user_id=job["user_id"])
    assert [session["delete_block_reason"] for session in sessions["sessions"]] == [None]


async def test_a_request_opens_one_snapshot_and_returns_it_while_it_is_on_its_way(store, pool):
    job, sandbox = await _ended_turn(store)
    before = await _group(pool, sandbox["sandbox_id"])

    opened = await _request(store, job)

    assert opened["requested"] is True and opened["state"] == "pending"
    after = await _group(pool, sandbox["sandbox_id"])
    assert after == {"workspace_epoch": before["workspace_epoch"] + 1, "snapshot_state": "pending"}
    view = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert view is not None
    assert view["workspace_publication_state"] == "pending"
    assert view["workspace_unpublished_changes"] is False
    # Asked again while it is on its way, at any stage: the same snapshot.
    assert await _request(store, job) == {**opened, "requested": False}
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None and capture["snapshot_id"] == opened["snapshot_id"]
    assert capture["workspace_epoch"] == after["workspace_epoch"]
    assert capture["anchor_job_id"] == job["id"]
    assert await _request(store, job) == {
        "snapshot_id": opened["snapshot_id"],
        "state": "capturing",
        "requested": False,
    }
    assert (await _group(pool, sandbox["sandbox_id"]))["workspace_epoch"] == after[
        "workspace_epoch"
    ]


async def test_a_failed_publication_leaves_the_workspace_unpublished(store, pool):
    job, _sandbox = await _ended_turn(store)
    assert (await _request(store, job))["requested"] is True
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=_PATCH,
            content_sha256=hashlib.sha256(_PATCH.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    await store.snapshot.fail_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        detail="GitHub rejected the draft PR (422): the base branch is invalid or unavailable",
    )

    view = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert view is not None
    assert view["workspace_publication_state"] == "failed"
    assert view["workspace_unpublished_changes"] is True


async def test_a_request_is_refused_for_what_its_owner_cannot_publish(store, pool):
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-refusals", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    # A turn running in the workspace: publish once it has ended.
    assert await _request(store, job) == {"error": "turn_running"}
    # Someone else's conversation, or none at all.
    assert await _request(store, job, user_id="someone-else") == {"error": "not_found"}
    assert await store.snapshot.request_snapshot_publication(
        thread_id="thread-that-does-not-exist", user_id=job["user_id"]
    ) == {"error": "not_found"}
    # A conversation that has not run a turn yet has no workspace to publish.
    unstarted = await _create_job(store, task_prompt="not started yet")
    assert await _request(store, unstarted) == {"error": "nothing_to_publish"}
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_sandbox_snapshots WHERE sandbox_id = $1",
                sandbox["sandbox_id"],
            )
            == 0
        )


async def test_a_workspace_being_deleted_cannot_be_published(store, pool):
    job, sandbox = await _ended_turn(store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET deletion_state = 'pending' WHERE id = $1",
            sandbox["sandbox_id"],
        )
    assert await _request(store, job) == {"error": "workspace_unavailable"}


async def test_a_deleted_conversation_cannot_be_published(store, pool):
    job, _sandbox = await _ended_turn(store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", job["thread_id"]
        )
    assert await _request(store, job) == {"error": "not_found"}


async def test_a_temporary_workspace_has_nothing_to_publish(store, pool):
    job, claim, sandbox = await _reserve_shared_chat(
        store, repo=None, base_sha=TEMPORARY_WORKSPACE_BASE_SHA
    )
    await _run_turn(store, job, claim, sandbox)

    assert await _request(store, job) == {"error": "temporary_workspace"}
    view = await store.sandbox.thread_sandbox(thread_id=job["thread_id"])
    assert view is not None
    assert view["workspace_unpublished_changes"] is False
