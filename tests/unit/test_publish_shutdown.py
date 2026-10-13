"""A control-plane stop hands an in-flight publication straight back to the queue.

The lifespan cancels the publisher at shutdown. A snapshot it had claimed is in
``publishing``, which only the stalled-publish reaper reclaims, fifteen minutes
later, so every restart that landed during a push or a PR call delayed that
workspace's publication -- and every later epoch of it -- by as much. Replaying
a publication is safe however far it got, so the claim goes back at once.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.support.storage import AllCapabilities

from cloud_agent import publish_worker
from cloud_agent.publish_worker import GitHubCredential, publish_one
from cloud_agent.publisher import PublishResult

pytestmark = pytest.mark.asyncio

_CLAIM = {
    "publication_kind": "shared_snapshot",
    "snapshot_id": 9,
    "publish_attempt_count": 2,
    "workspace_epoch": 4,
    "included_job_ids": ["ajob_1"],
    "anchor_job_id": "ajob_1",
    "thread_id": "athr_1",
    "branch_id": "athr_1",
    "job_id": "ajob_1",
    "user_id": "u1",
    "repo": "o/n",
    "base_sha": "abc123",
    "task_prompt": "fix the flaky test",
    "session_summaries": ["Stabilize test retries"],
    "patch": "diff --git a/x b/x\n",
}


class _Snapshots(AllCapabilities):
    """One claimed snapshot, and what the publisher then did with it."""

    def __init__(self) -> None:
        self._claim: dict[str, Any] | None = dict(_CLAIM)
        self.renew_snapshot_publish_claim = AsyncMock(return_value=True)
        self.defer_snapshot_publish = AsyncMock(return_value=True)
        self.fail_snapshot_publish = AsyncMock()
        self.record_snapshot_publish = AsyncMock(return_value=True)

    async def claim_for_publish(self) -> dict[str, Any] | None:
        claim, self._claim = self._claim, None
        return claim


async def test_a_publish_cancelled_mid_flight_returns_its_claim_to_the_queue(monkeypatch) -> None:
    """Cancelled while GitHub is answering, the claim is deferred under its fence."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "o/n")
    monkeypatch.setattr(
        publish_worker,
        "publish_patch",
        lambda **_kwargs: PublishResult(branch="agent/athr_1", commit_sha="s", changed_files=["x"]),
    )
    asked = asyncio.Event()

    async def pull_request_in_flight(**_kwargs: Any) -> str:
        asked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request_in_flight)
    store = _Snapshots()
    task = asyncio.create_task(publish_one(store, credential=GitHubCredential("t")))
    await asyncio.wait_for(asked.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    store.defer_snapshot_publish.assert_awaited_once()
    deferred = store.defer_snapshot_publish.await_args.kwargs
    assert (deferred["snapshot_id"], deferred["expected_publish_attempt_count"]) == (9, 2)
    store.fail_snapshot_publish.assert_not_awaited()
    store.record_snapshot_publish.assert_not_awaited()
