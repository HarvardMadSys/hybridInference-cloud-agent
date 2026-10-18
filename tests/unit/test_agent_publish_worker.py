"""Unit tests for the publish step (patch → branch → draft PR).

This is the hop that produces the visible product outcome, and the one where a
mistake is externally visible, so the cases below are the ones that would show
up in someone's repository: double-publishing, publishing a blocked patch, and
opening a PR that is not a draft.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from tests.support.storage import AllCapabilities

from cloud_agent import github_app, publish_worker
from cloud_agent.entitlement import RepoAccessUnavailable
from cloud_agent.github_app import AppNotInstalled, GitHubAppError
from cloud_agent.publish_worker import (
    GitHubCredential,
    create_draft_pull_request,
    ensure_draft_pull_request,
    publish_loop,
    publish_one,
)
from cloud_agent.publisher import PublishError, PublishResult, TransientPublishError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _entitled(monkeypatch):
    """Entitle the fixture repository.

    Publishing now re-checks entitlement before minting a write credential, so
    a test that does not declare one is correctly refused.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "o/n")


_JOB = {
    "publication_kind": "shared_snapshot",
    "snapshot_id": 9,
    "publish_attempt_count": 0,
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
    # The group's container as the publication claim read it.
    "generation": 1,
    "container_id": "container-1",
}


class FakeStore(AllCapabilities):
    """Records what the publisher did to the job."""

    def __init__(self, job: dict[str, Any] | None = None, *, record_ok: bool = True) -> None:
        self._job = job
        self.claims = 0
        self.snapshot_recorded: tuple[int, str, str] | None = None
        self.snapshot_failed: tuple[int, str] | None = None
        self.snapshot_deferred: tuple[int, str] | None = None
        self.record_expected_attempt: int | None = None
        self.failed_expected_attempt: int | None = None
        self.deferred_expected_attempt: int | None = None
        self.renewed_expected_attempts: list[int] = []
        self.renew_ok = True
        self.import_failed: tuple[int, str] | None = None
        self.import_completed: tuple[int, str] | None = None
        self.import_completed_for: tuple[int, str | None] | None = None
        self.pushes: list[tuple[int, int, str]] = []
        self.push_record: bool | Exception = True
        self.deferred_delay_s: float | None = None
        self.import_failed_delay_s: float | None = None
        self._record_ok = record_ok

    async def claim_for_publish(self):
        self.claims += 1
        job, self._job = self._job, None
        return job

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """The fixture has no connected installations beyond its allowlist."""
        return []

    async def renew_snapshot_publish_claim(
        self, *, snapshot_id: int, expected_publish_attempt_count: int
    ) -> bool:
        assert snapshot_id == 9
        self.renewed_expected_attempts.append(expected_publish_attempt_count)
        return self.renew_ok

    async def record_snapshot_push(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, commit_sha: str
    ) -> bool:
        if isinstance(self.push_record, Exception):
            raise self.push_record
        if self.push_record:
            self.pushes.append((snapshot_id, expected_publish_attempt_count, commit_sha))
        return self.push_record

    async def record_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        expected_publish_attempt_count: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        self.record_expected_attempt = expected_publish_attempt_count
        self.snapshot_recorded = (snapshot_id, pr_url, commit_sha)
        return self._record_ok

    async def fail_snapshot_publish(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, detail: str
    ) -> None:
        self.failed_expected_attempt = expected_publish_attempt_count
        self.snapshot_failed = (snapshot_id, detail)

    async def defer_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        expected_publish_attempt_count: int,
        detail: str,
        delay_s: float | None = None,
    ) -> bool:
        self.deferred_expected_attempt = expected_publish_attempt_count
        self.snapshot_deferred = (snapshot_id, detail)
        self.deferred_delay_s = delay_s
        return True

    async def pending_snapshot_base_import(self) -> dict[str, Any] | None:
        # No durable import is outstanding: these scenarios publish from a
        # sandbox manager that answered. The publisher used to reach for this through
        # `getattr` on the store, so a fake missing it read as "nothing
        # pending" instead of as a fake that had drifted from its port.
        return None

    async def record_snapshot_base_import_failure(
        self, *, snapshot_id: int, detail: str, delay_s: float | None = None
    ) -> None:
        self.import_failed = (snapshot_id, detail)
        self.import_failed_delay_s = delay_s

    async def complete_snapshot_base_import(
        self,
        *,
        snapshot_id: int,
        commit_sha: str,
        generation: int,
        container_id: str | None,
    ) -> bool:
        self.import_completed = (snapshot_id, commit_sha)
        self.import_completed_for = (generation, container_id)
        return True


def _patch_publish(monkeypatch, result_or_exc):
    """Stand in for the git-side publisher."""

    def fake(**kwargs):
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(publish_worker, "publish_patch", fake)


def _patch_pr(monkeypatch, url: str = "https://github.com/o/n/pull/7"):
    """Stand in for the GitHub API call, capturing its arguments."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return url

    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", fake)
    return captured


@pytest.mark.parametrize(
    "detail",
    [
        "patch rejected: patch introduces symlinks: docs/x(503)",
        "patch rejected after apply: credential-shaped content in 'connection reset.md'",
    ],
)
async def test_a_rejected_patch_is_never_retried_whatever_its_paths_say(detail):
    """A path quoted in the detail must not read as an HTTP status or a disconnect."""
    from cloud_agent.publisher import PatchRejected

    assert not publish_worker._is_retryable_publication_failure(PatchRejected(detail))
    # The same text from the provider or git is still classified by what it says.
    assert publish_worker._is_retryable_publication_failure(PublishError(detail))


async def test_empty_queue_is_a_no_op():
    """Nothing pending means nothing happens."""
    store = FakeStore(None)
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_recorded is None and store.snapshot_failed is None


@pytest.mark.parametrize(
    ("status", "headers", "body", "wait"),
    [
        (403, {"retry-after": "60"}, {"message": "Forbidden"}, 60.0),
        (429, {"retry-after": "7"}, {}, 7.0),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1000600"}, {}, 600.0),
        (403, {"x-ratelimit-remaining": "0"}, {"message": "Forbidden"}, 0.0),
        (403, {}, {"message": "You have exceeded a secondary rate limit."}, 0.0),
        (429, {}, {}, 0.0),
    ],
)
async def test_a_rate_limited_refusal_from_github_waits_as_long_as_it_asks(
    monkeypatch, status, headers, body, wait
):
    """GitHub answers its rate limits with 403 as well as 429; waiting lifts them."""
    monkeypatch.setattr(github_app.time, "time", lambda: 1_000_000.0)
    error = publish_worker._github_api_error(
        "the draft PR", httpx.Response(status, headers=headers, json=body)
    )
    assert isinstance(error, TransientPublishError)
    assert error.retry_after_s == wait
    forbidden = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    assert not isinstance(
        publish_worker._github_api_error("the draft PR", forbidden), TransientPublishError
    )
    outage = publish_worker._github_api_error("the draft PR", httpx.Response(503))
    assert isinstance(outage, TransientPublishError) and outage.retry_after_s is None


async def test_a_rate_limited_pull_request_step_is_retried_after_githubs_wait(monkeypatch):
    """The deferral carries the wait; storage, not a fixed interval, decides when."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    limited = TransientPublishError("GitHub refused the PR lookup (403)", retry_after_s=1800.0)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", AsyncMock(side_effect=limited))

    assert await publish_one(store, credential=GitHubCredential("t")) is None

    assert store.snapshot_deferred == (9, "GitHub refused the PR lookup (403)")
    assert store.deferred_delay_s == 1800.0
    assert store.snapshot_failed is None


async def test_a_rate_limited_credential_mint_is_retried_after_githubs_wait(monkeypatch):
    """A 403 from the token mint is permanent unless GitHub says it is its rate limit."""
    store = FakeStore(dict(_JOB))
    limited = GitHubAppError("GitHub POST mint failed (403)", status=403, retry_after_s=90.0)

    class App:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            raise limited

    monkeypatch.setattr(publish_worker, "publish_patch", Mock())
    assert await publish_one(store, app_credentials=App()) is None

    assert store.snapshot_deferred is not None and store.snapshot_failed is None
    assert store.deferred_delay_s == 90.0
    assert not publish_worker._is_retryable_publication_failure(
        GitHubAppError("GitHub POST mint failed (403)", status=403)
    )


async def test_the_app_names_githubs_rate_limit_wait_on_its_errors(monkeypatch):
    """The token mint goes through the App's request helper, which keeps the wait."""
    monkeypatch.setattr(github_app.time, "time", lambda: 1_000_000.0)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1000120"},
            json={"message": "API rate limit exceeded"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        credentials = github_app.GitHubAppCredentials(
            github_app.AppConfig(app_id="1", private_key="k", api_base="https://api.test"),
            client=client,
        )
        with pytest.raises(GitHubAppError) as raised:
            await credentials._request("POST", "/app/installations/1/access_tokens", token="jwt")
    assert raised.value.status == 403
    assert raised.value.retry_after_s == 120.0


async def test_a_push_stays_recorded_when_its_pull_request_step_fails(monkeypatch):
    """The next publication from this base receives it as the group's own push."""
    store = FakeStore(dict(_JOB, own_pushes=["c0ffee"]))
    seen: dict[str, Any] = {}

    def publish(**kwargs):
        seen.update(kwargs)
        kwargs["before_push"]("pushed")
        return PublishResult(branch="agent/athr_1", commit_sha="pushed", changed_files=["x"])

    async def refuse(**_kwargs):
        raise PublishError("GitHub refused the draft PR (403)")

    monkeypatch.setattr(publish_worker, "publish_patch", publish)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", refuse)

    assert await publish_one(store, credential=GitHubCredential("t")) is None

    assert seen["own_pushes"] == ("c0ffee",)
    assert store.pushes == [(9, 0, "pushed")]
    assert store.snapshot_failed is not None and store.snapshot_recorded is None


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (OSError("database unavailable"), "could not record the commit before pushing it"),
        (False, "the publication claim was lost before the push"),
    ],
)
async def test_a_commit_that_cannot_be_recorded_is_not_pushed(monkeypatch, record, reason):
    """An unrecorded push would read as somebody else's to the next publication."""
    store = FakeStore(dict(_JOB))
    store.push_record = record
    pushed: list[str] = []

    def publish(**kwargs):
        kwargs["before_push"]("c0ffee")
        pushed.append("c0ffee")
        return PublishResult(branch="agent/athr_1", commit_sha="c0ffee", changed_files=["x"])

    pull_request = AsyncMock()
    monkeypatch.setattr(publish_worker, "publish_patch", publish)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request)

    assert await publish_one(store, credential=GitHubCredential("t")) is None

    assert pushed == [] and store.pushes == []
    pull_request.assert_not_awaited()
    # Retried under the same fence: a lost claim's deferral is refused by storage.
    assert store.snapshot_deferred is not None and store.snapshot_failed is None
    assert reason in store.snapshot_deferred[1]


async def test_successful_publish_records_the_pr(monkeypatch):
    """A clean patch becomes a branch, a draft PR, and a recorded URL."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    url = await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")
    assert url == "https://github.com/o/n/pull/7"
    assert store.snapshot_recorded == (9, url, "s")
    assert captured["head_branch"] == "agent/ajob_1"
    assert captured["base_branch"] == "dev"
    assert captured["title"] == "[agent] Stabilize test retries"
    assert "ajob_1" in captured["body"]


async def test_slow_publication_keeps_renewing_its_fenced_claim(monkeypatch):
    """The heartbeat spans provider waits, not merely claim and final write."""
    store = FakeStore(dict(_JOB))
    started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(publish_worker, "_PUBLISH_HEARTBEAT_INTERVAL_S", 0.001)
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)

    class App:
        async def token_for(self, _repo: str, **_kwargs: Any) -> str:
            started.set()
            await release.wait()
            return "t"

    pending = asyncio.create_task(publish_one(store, app_credentials=App()))
    await asyncio.wait_for(started.wait(), timeout=1)
    for _ in range(50):
        if len(store.renewed_expected_attempts) >= 2:
            break
        await asyncio.sleep(0.002)

    assert store.renewed_expected_attempts[:2] == [0, 0]
    release.set()
    assert await pending == "https://github.com/o/n/pull/7"
    renewals = len(store.renewed_expected_attempts)
    await asyncio.sleep(0.005)
    assert len(store.renewed_expected_attempts) == renewals, "the heartbeat task leaked"


async def test_a_stale_publisher_stops_before_any_remote_side_effect(monkeypatch):
    """A reaper-owned replacement fences the old worker at its first renewal."""
    store = FakeStore(dict(_JOB))
    store.renew_ok = False
    push = Mock()
    pull_request = AsyncMock()
    monkeypatch.setattr(publish_worker, "publish_patch", push)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request)

    assert await publish_one(store, credential=GitHubCredential("t")) is None

    assert store.renewed_expected_attempts == [0]
    assert store.snapshot_recorded is None
    assert store.snapshot_deferred is None
    assert store.snapshot_failed is None
    push.assert_not_called()
    pull_request.assert_not_awaited()


async def test_publishing_asks_for_a_token_that_outlives_the_publish(monkeypatch):
    """A cached token is served until five minutes before expiry; a publish can take longer."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/athr_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)
    asked: dict[str, Any] = {}

    class App:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            asked.update(kwargs)
            return "ghs_fresh"

    await publish_one(store, app_credentials=App())

    assert asked["min_ttl_s"] >= 900


async def test_cancelling_a_publish_while_its_heartbeat_stops_is_not_swallowed(monkeypatch):
    """A shutdown that lands while the heartbeat winds down must still stop the publisher."""
    beating = asyncio.Event()
    stopping = asyncio.Event()
    release = asyncio.Event()

    async def heartbeat(_store: Any, **_fence: Any) -> None:
        beating.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await release.wait()
            raise

    async def open_pr(**_kwargs: Any) -> str:
        # Finish only once the heartbeat runs, as it does under any real push.
        await beating.wait()
        return "https://github.com/o/n/pull/7"

    monkeypatch.setattr(publish_worker, "_heartbeat_snapshot_publish", heartbeat)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", open_pr)
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/athr_1", commit_sha="s", changed_files=["x"])
    )
    task = asyncio.create_task(publish_one(FakeStore(dict(_JOB)), credential=GitHubCredential("t")))
    await stopping.wait()

    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_default_publish_branch_uses_current_saved_setting(monkeypatch):
    values = {"AGENT_PUBLISH_BASE_BRANCH": "first"}
    monkeypatch.setattr(
        publish_worker, "setting_env", lambda name, default=None: values.get(name, default)
    )
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)
    await publish_one(FakeStore(dict(_JOB)), credential=GitHubCredential("t"))
    assert captured["base_branch"] == "first"
    values["AGENT_PUBLISH_BASE_BRANCH"] = "second"
    await publish_one(FakeStore(dict(_JOB)), credential=GitHubCredential("t"))
    assert captured["base_branch"] == "second"


@pytest.mark.parametrize(
    ("summaries", "expected"),
    [
        (
            ["## Summary\n\n- **Fix** [`retry.py`](/workspace/retry.py) timeouts"],
            "Fix retry.py timeouts",
        ),
        (
            ["Changes:\n```python\nraise TimeoutError\n```\n\nFixed\t retry   timeouts"],
            "Fixed retry timeouts",
        ),
        (["Fix retries", "Add retry docs", "Fix retries"], "Fix retries; Add retry docs"),
        (
            ["Fix retries " + "with backoff " * 20, "Add retry docs"],
            "Fix retries with backoff with bac…; Add retry docs",
        ),
        (
            [
                "Fix retry",
                "Add retry docs",
                "Update API",
                "Refresh cache",
                "Fix exports",
                "Add logging",
            ],
            "Fix retry; Add retry d…; Update API; Refresh cac…; +2 more",
        ),
        (["修复重试超时"], "修复重试超时"),
        (["修" * 100], "修" * 69 + "…"),
        (["## Summary\n\n"], "Update x"),
        (["\x1bmalformed\u202e title", None, {"title": "bad"}], "Update x"),
        (["(the agent finished without a closing message)"], "Update x"),
        (["Done."], "Update x"),
        (
            [
                "I've implemented the feature. Here's a summary of what I did.\n\n"
                "- Add a button to restart failed runs in a new session"
            ],
            "Add a button to restart failed runs in a new session",
        ),
        (["I've implemented the feature. Here's a summary of what I did."], "Update x"),
        (
            ["I've implemented the feature.\n\nAdded exponential retry backoff."],
            "Added exponential retry backoff.",
        ),
        (["Implemented exponential retry backoff"], "Implemented exponential retry backoff"),
        (["Changes.ts: add restart navigation"], "Changes.ts: add restart navigation"),
        (["summary.py now preserves earlier turns"], "summary.py now preserves earlier turns"),
        (["⏳ The model is starting up — please wait"], "Update x"),
        ([], "Update x"),
        (None, "Update x"),
    ],
)
async def test_pr_title_uses_readable_closing_summaries(monkeypatch, summaries, expected):
    """Publication names completed work, even when the request differs or is long."""
    prompt = "Please fix the problem I described " * 100
    store = FakeStore({**_JOB, "task_prompt": prompt, "session_summaries": summaries})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"))

    assert captured["title"] == f"[agent] {expected}"
    assert len(captured["title"]) <= 78
    assert "Please fix" not in captured["title"]


async def test_title_generation_failure_publishes_a_meaningful_fallback(monkeypatch):
    closing = (
        "I've implemented the feature. Here's a summary of what I did.\n\n"
        "- Add a button to restart failed runs in a new session"
    )
    store = FakeStore({**_JOB, "session_summaries": [closing]})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)
    generated = AsyncMock(return_value=None)
    monkeypatch.setattr(publish_worker, "generate_pr_title", generated)

    url = await publish_one(store, credential=GitHubCredential("t"))

    assert captured["title"] == "[agent] Add a button to restart failed runs in a new session"
    assert store.snapshot_recorded == (9, url, "s")
    generated.assert_awaited_once()


async def test_shared_snapshot_publishes_group_lineage_and_imports_the_new_base(monkeypatch):
    from cloud_agent import sandbox_manager_routing

    snapshot = {
        **_JOB,
        "publication_kind": "shared_snapshot",
        "snapshot_id": 9,
        "workspace_epoch": 4,
        "included_job_ids": ["ajob_1", "ajob_2"],
        "anchor_job_id": "ajob_2",
        "thread_id": "athr_child",
        "branch_id": "athr_origin",
    }
    store = FakeStore(snapshot)
    _patch_publish(
        monkeypatch,
        PublishResult(
            branch="agent/athr_origin",
            commit_sha="b" * 40,
            changed_files=["x"],
            base_bundle=b"bundle",
        ),
    )
    captured_pr = _patch_pr(monkeypatch)

    class SandboxManager:
        def __init__(self) -> None:
            self.imported: tuple[dict[str, Any], str, bytes] | None = None

        async def import_shared_base(self, row, *, commit_sha: str, bundle: bytes):
            self.imported = (row, commit_sha, bundle)

    sandbox_manager = SandboxManager()

    async def route(_store, job_id: str):
        assert job_id == "ajob_2"
        return sandbox_manager

    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", route)

    url = await publish_one(store, credential=GitHubCredential("t"))

    assert url == "https://github.com/o/n/pull/7"
    assert store.snapshot_recorded == (9, url, "b" * 40)
    assert store.snapshot_failed is None and store.import_failed is None
    assert store.import_completed == (9, "b" * 40)
    # Completed for the container the import was sent to, and no other.
    assert store.import_completed_for == (1, "container-1")
    assert sandbox_manager.imported == (snapshot, "b" * 40, b"bundle")
    assert "group snapshot" in captured_pr["body"]
    assert "ajob_1" in captured_pr["body"] and "ajob_2" in captured_pr["body"]


async def test_a_retried_base_import_bundles_against_the_workspaces_first_base(monkeypatch):
    """The workspace keeps the commit it started from; Git prunes published ones together.

    A published commit is imported as objects no ref names, and Git prunes
    the commits published before it in the same pass. A bundle that needs the
    previous published base can then never be unbundled again; one cut from
    the base the group's first snapshot started from can, because the
    workspace's own history holds that commit.
    """
    from cloud_agent import sandbox_manager_routing

    store = FakeStore()
    fetched: dict[str, Any] = {}

    def fetch(**kwargs: Any) -> bytes:
        fetched.update(kwargs)
        return b"bundle"

    class SandboxManager:
        imported: tuple[str, bytes] | None = None

        async def import_shared_base(self, row, *, commit_sha: str, bundle: bytes):
            self.imported = (commit_sha, bundle)

    sandbox_manager = SandboxManager()

    async def route(_store, job_id: str):
        assert job_id == "ajob_1"
        return sandbox_manager

    monkeypatch.setattr(publish_worker, "fetch_base_bundle", fetch)
    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", route)
    pending = {
        "snapshot_id": 9,
        "anchor_job_id": "ajob_1",
        "repo": "o/n",
        "user_id": "u1",
        "published_commit_sha": "c" * 40,
        "base_sha": "b" * 40,
        "origin_base_sha": "a" * 40,
        "generation": 2,
        "container_id": "container-2",
    }

    assert await publish_worker._retry_base_import(
        store, pending, credential=GitHubCredential("t"), app_credentials=None
    )

    assert fetched["commit_sha"] == "c" * 40
    assert fetched["base_sha"] == "a" * 40
    assert sandbox_manager.imported == ("c" * 40, b"bundle")
    assert store.import_completed == (9, "c" * 40)
    assert store.import_completed_for == (2, "container-2")
    assert store.import_failed is None


async def test_shared_snapshot_rejection_does_not_fail_its_successful_turn(monkeypatch):
    snapshot = {
        **_JOB,
        "publication_kind": "shared_snapshot",
        "snapshot_id": 9,
        "workspace_epoch": 4,
        "included_job_ids": ["ajob_1"],
        "anchor_job_id": "ajob_1",
        "branch_id": "athr_origin",
    }
    store = FakeStore(snapshot)
    _patch_publish(monkeypatch, PublishError("patch rejected"))

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_failed == (9, "patch rejected")


async def test_the_pr_targets_the_branch_the_job_was_started_from(monkeypatch):
    """A job pinned from `release/x` must not propose merging into `dev`.

    The deployment default is a fallback, not an override. Targeting it
    regardless showed every commit between the two branches as part of the
    agent's change, and proposed the merge into the wrong line of development.
    """
    store = FakeStore({**_JOB, "metadata": {"_agent_base_ref": "release/x"}})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert captured["base_branch"] == "release/x"


async def test_a_job_that_named_no_branch_targets_the_deployment_default(monkeypatch):
    """Most jobs name no branch, and those keep the configured default."""
    store = FakeStore({**_JOB, "metadata": {"ticket": "ABC-1"}})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert captured["base_branch"] == "dev"


async def test_a_base_ref_that_cannot_be_a_pr_base_falls_back(monkeypatch):
    """A commit hash is not a branch, so GitHub would refuse the PR.

    The API resolves any ref it can, so a caller may have named a commit. That
    is not a reason to throw away a finished job's work: the default still
    opens a reviewable draft.
    """
    store = FakeStore({**_JOB, "metadata": {"_agent_base_ref": "a" * 40}})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert captured["base_branch"] == "dev"


async def test_follow_up_fast_forwards_the_thread_branch_without_opening_another_pr(monkeypatch):
    """Follow-ups refresh the branch's open PR and its session summary."""
    job = {
        **_JOB,
        "job_id": "ajob_2",
        "anchor_job_id": "ajob_2",
        "included_job_ids": ["ajob_1", "ajob_2"],
        "thread_id": "athr_1",
        "parent_job_id": "ajob_1",
        "parent_pr_url": "https://github.com/o/n/pull/7",
    }
    store = FakeStore(job)
    captured_publish: dict[str, Any] = {}

    def fake_publish(**kwargs):
        captured_publish.update(kwargs)
        return PublishResult(branch="agent/athr_1", commit_sha="next-sha", changed_files=["x"])

    monkeypatch.setattr(publish_worker, "publish_patch", fake_publish)
    pr_called = _patch_pr(monkeypatch)

    url = await publish_one(store, credential=GitHubCredential("t"))

    assert url == "https://github.com/o/n/pull/7"
    assert captured_publish["branch_id"] == "athr_1"
    assert pr_called["head_branch"] == "agent/athr_1"
    assert pr_called["title"] == "[agent] Stabilize test retries"
    assert store.snapshot_recorded == (9, url, "next-sha")


@pytest.mark.parametrize("prior_state", ["open", "closed", "merged", "missing", "invalid_link"])
async def test_publication_resolves_the_current_branch_pr(monkeypatch, prior_state):
    """A closed or merged PR cannot trap the next publication on its old URL."""
    requests = []
    old_url = "https://github.com/o/n/pull/7"
    new_url = "https://github.com/o/n/pull/8"

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer t"
        if request.method == "GET":
            assert dict(request.url.params) == {"state": "open", "head": "o:agent/athr_1"}
            return httpx.Response(
                200,
                json=[{"number": 7, "state": "open", "html_url": old_url}]
                if prior_state == "open"
                else [],
            )
        if request.method == "PATCH":
            assert request.url.path == "/repos/o/n/pulls/7"
            assert (
                json.loads(request.content)["title"] == "[agent] Fix retries and document backoff"
            )
            return httpx.Response(200, json={"state": "open", "html_url": old_url})
        assert request.method == "POST"
        assert json.loads(request.content)["draft"] is True
        return httpx.Response(201, json={"html_url": new_url})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/athr_1", commit_sha="next", changed_files=["x"])
    )

    async def generated(*args, **kwargs):
        return "[agent] Fix retries and document backoff"

    monkeypatch.setattr(publish_worker, "generate_pr_title", generated)
    prior_url = (
        "https://example.test/somewhere"
        if prior_state == "invalid_link"
        else old_url
        if prior_state != "missing"
        else None
    )
    store = FakeStore({**_JOB, "parent_pr_url": prior_url})
    url = await publish_one(store, credential=GitHubCredential("t"))

    assert url == (old_url if prior_state == "open" else new_url)
    assert store.snapshot_recorded == (9, url, "next")
    assert [request.method for request in requests] == [
        "GET",
        "PATCH" if prior_state == "open" else "POST",
    ]


async def test_a_draft_retargeted_by_a_reviewer_is_refreshed_not_duplicated(monkeypatch):
    """GitHub allows open PRs from one branch to several bases; there must stay one."""
    requests: list[httpx.Request] = []
    open_prs = [
        {
            "number": 7,
            "state": "open",
            "html_url": "https://github.com/o/n/pull/7",
            "base": {"ref": "main"},
        }
    ]

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            wanted = request.url.params.get("base")
            return httpx.Response(
                200, json=[pr for pr in open_prs if wanted in (None, pr["base"]["ref"])]
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={"state": "open"})
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/8"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    url = await ensure_draft_pull_request(
        repo="o/n",
        head_branch="agent/athr_1",
        base_branch="dev",
        title="[agent] Fix retries",
        body="Summary",
        credential=GitHubCredential("t"),
    )

    assert url == "https://github.com/o/n/pull/7"
    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert json.loads(requests[1].content) == {"title": "[agent] Fix retries", "body": "Summary"}


async def test_pr_closed_during_update_creates_a_new_draft(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"number": 7, "state": "open", "html_url": "https://github.com/o/n/pull/7"}],
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={"state": "closed"})
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/8"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    url = await ensure_draft_pull_request(
        repo="o/n",
        head_branch="agent/athr_1",
        base_branch="dev",
        title="[agent] Fix retries",
        body="Summary",
        credential=GitHubCredential("t"),
    )
    assert url == "https://github.com/o/n/pull/8"
    assert requests == ["GET", "PATCH", "POST"]


@pytest.mark.parametrize(
    "create_response",
    [
        pytest.param(httpx.Response(201, json={}), id="response-lost"),
        pytest.param(
            httpx.Response(422, json={"message": "A pull request already exists"}),
            id="create-race",
        ),
    ],
)
async def test_retry_reconciles_an_uncertain_or_duplicate_pr_create(monkeypatch, create_response):
    """A later lookup reconciles create uncertainty instead of duplicating it."""
    requests: list[str] = []
    lookup_count = 0
    pr_url = "https://github.com/o/n/pull/7"

    def handle(request):
        nonlocal lookup_count
        requests.append(request.method)
        if request.method == "GET":
            lookup_count += 1
            if lookup_count == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"number": 7, "state": "open", "html_url": pr_url}],
            )
        if request.method == "POST":
            return create_response
        assert request.method == "PATCH"
        return httpx.Response(200, json={"state": "open", "html_url": pr_url})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    _patch_publish(
        monkeypatch,
        PublishResult(branch="agent/athr_1", commit_sha="next", changed_files=["x"]),
    )

    first = FakeStore(dict(_JOB))
    assert await publish_one(first, credential=GitHubCredential("t")) is None
    assert first.snapshot_deferred is not None
    assert first.snapshot_failed is None
    assert first.deferred_expected_attempt == 0

    restarted = FakeStore({**_JOB, "publish_attempt_count": 1})
    assert await publish_one(restarted, credential=GitHubCredential("t")) == pr_url
    assert restarted.snapshot_recorded == (9, pr_url, "next")
    assert restarted.record_expected_attempt == 1
    assert requests == ["GET", "POST", "GET", "PATCH"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(408, text="request timeout"),
        httpx.Response(429, text="rate limited"),
        httpx.Response(500, text="server error"),
        httpx.Response(599, text="network connect timeout"),
        httpx.Response(201, text="not json"),
        httpx.ConnectError("create response lost"),
    ],
)
async def test_pr_create_transient_failures_requeue(monkeypatch, response):
    """An uncertain create outcome is reconciled by a later lookup."""
    requests: list[str] = []

    def handle(request):
        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert request.method == "POST"
        if isinstance(response, Exception):
            raise response
        return response

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    _patch_publish(
        monkeypatch,
        PublishResult(branch="agent/athr_1", commit_sha="next", changed_files=["x"]),
    )
    store = FakeStore(dict(_JOB))

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_recorded is None
    assert store.snapshot_failed is None
    assert store.snapshot_deferred is not None
    assert requests == ["GET", "POST"]


@pytest.mark.parametrize("failure", ["lookup", "update"])
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(408, text="request timeout"),
        httpx.Response(429, text="rate limited"),
        httpx.Response(500, text="server error"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(504, text="gateway timeout"),
        httpx.Response(599, text="network connect timeout"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": "object"}),
        httpx.Response(200, json=["not", "a", "PR"]),
        httpx.ConnectError("unreachable"),
    ],
)
async def test_pr_api_failure_does_not_record_the_stale_url(monkeypatch, failure, response):
    """Unknown PR state must neither create duplicates nor record a stale link."""
    requests = []

    def handle(request):
        requests.append(request.method)
        assert request.method != "POST"
        if request.method == "GET" and failure == "update":
            return httpx.Response(
                200,
                json=[{"number": 7, "state": "open", "html_url": "https://github.com/o/n/pull/7"}],
            )
        if isinstance(response, Exception):
            raise response
        return response

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/athr_1", commit_sha="next", changed_files=["x"])
    )
    store = FakeStore({**_JOB, "parent_pr_url": "https://github.com/o/n/pull/7"})
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_recorded is None
    retryable = not (isinstance(response, httpx.Response) and response.status_code == 404)
    recorded = store.snapshot_deferred if retryable else store.snapshot_failed
    assert recorded is not None
    assert store.snapshot_failed is None if retryable else store.snapshot_deferred is None
    if isinstance(response, httpx.ConnectError):
        assert "unreachable" in recorded[1]
    elif isinstance(response, httpx.Response) and response.status_code >= 400:
        assert str(response.status_code) in recorded[1]
    assert requests == (["GET", "PATCH"] if failure == "update" else ["GET"])


@pytest.mark.parametrize("attempts", [4, 5])
async def test_transient_publication_delegates_the_attempt_budget_to_storage(monkeypatch, attempts):
    """Every transient failure uses the claim fence; storage decides exhaustion."""
    store = FakeStore({**_JOB, "publish_attempt_count": attempts})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    monkeypatch.setattr(
        publish_worker,
        "ensure_draft_pull_request",
        AsyncMock(side_effect=PublishError("GitHub refused the PR lookup (503)")),
    )

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_recorded is None
    assert store.snapshot_failed is None
    assert store.snapshot_deferred == (9, "GitHub refused the PR lookup (503)")
    assert store.deferred_expected_attempt == attempts


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        (httpx.ConnectError("connection reset"), True),
        (httpx.ReadTimeout("timed out"), True),
        (GitHubAppError("GitHub token mint failed (408)", status=408), True),
        (GitHubAppError("GitHub token mint failed (429)", status=429), True),
        (GitHubAppError("GitHub token mint failed (500)", status=500), True),
        (GitHubAppError("GitHub token mint failed (502)", status=502), True),
        (GitHubAppError("GitHub token mint failed (503)", status=503), True),
        (GitHubAppError("GitHub token mint failed (504)", status=504), True),
        (GitHubAppError("GitHub token mint failed (599)", status=599), True),
        (GitHubAppError("GitHub token mint failed (401)", status=401), False),
        (AppNotInstalled("no App installation covers o/n", status=404), False),
        (GitHubAppError("could not sign the App JWT"), False),
    ],
)
async def test_credential_mint_retries_only_transient_github_failures(monkeypatch, error, retried):
    """A GitHub blip while minting must not fail the snapshot; a refusal must."""
    store = FakeStore(dict(_JOB))

    class App:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            raise error

    def forbid_push(**kwargs):
        raise AssertionError("a credential failure must not push")

    monkeypatch.setattr(publish_worker, "publish_patch", forbid_push)
    assert await publish_one(store, app_credentials=App()) is None
    assert store.snapshot_recorded is None
    recorded = store.snapshot_deferred if retried else store.snapshot_failed
    assert recorded is not None and recorded[0] == 9
    assert "could not obtain a GitHub credential" in recorded[1]
    assert store.snapshot_failed is None if retried else store.snapshot_deferred is None


@pytest.mark.parametrize(
    ("detail", "retried"),
    [
        (
            "git fetch failed: fatal: unable to access 'https://github.com/o/n.git/': "
            "Recv failure: Connection reset by peer",
            True,
        ),
        ("git fetch timed out after 120s", True),
        (
            "git push failed: fatal: unable to access 'https://github.com/o/n.git/': "
            "The requested URL returned error: 503",
            True,
        ),
        (
            "git fetch failed: fatal: Authentication failed for 'https://github.com/o/n.git/'",
            False,
        ),
        (
            "branch agent/athr_1 has moved: its tip is abcdefabcdef "
            "but this run built on 123456789abc",
            False,
        ),
    ],
)
async def test_git_transport_failures_retry_and_rejections_do_not(monkeypatch, detail, retried):
    """A disconnect while publishing is deferred; a real git refusal is not."""
    store = FakeStore(dict(_JOB))
    _patch_publish(monkeypatch, PublishError(detail))
    pr_called = _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert pr_called == {}
    assert store.snapshot_recorded is None
    recorded = store.snapshot_deferred if retried else store.snapshot_failed
    assert recorded is not None and recorded[0] == 9
    assert detail in recorded[1] or recorded[1] == detail
    assert store.snapshot_failed is None if retried else store.snapshot_deferred is None


async def test_gate_rejection_fails_the_job_and_never_pushes(monkeypatch):
    """A blocked patch is reported to the owner, not retried into the repo."""
    store = FakeStore(dict(_JOB))
    _patch_publish(monkeypatch, PublishError("patch rejected: modifies .github/"))
    pr_called = _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_recorded is None
    assert store.snapshot_failed is not None
    assert store.snapshot_failed[0] == 9
    assert ".github/" in store.snapshot_failed[1]
    assert pr_called == {}, "a rejected patch must never reach PR creation"


async def test_missing_base_sha_fails_instead_of_guessing(monkeypatch):
    """Without a base commit the patch has no defined target — refuse."""
    job = dict(_JOB)
    job["base_sha"] = None
    store = FakeStore(job)
    pr_called = _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_failed is not None
    assert "base_sha" in store.snapshot_failed[1]
    assert pr_called == {}


async def test_lost_race_does_not_claim_success(monkeypatch):
    """If another publisher recorded first, report nothing rather than a dupe."""
    store = FakeStore(dict(_JOB), record_ok=False)
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None


async def test_unexpected_failure_is_recorded_not_swallowed(monkeypatch):
    """An unexpected error still surfaces on the job instead of vanishing."""
    store = FakeStore(dict(_JOB))
    _patch_publish(monkeypatch, RuntimeError("disk full"))
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_failed is not None
    assert "disk full" in store.snapshot_failed[1]


async def test_pull_request_is_always_a_draft():
    """The PR must be a draft — human review is the safety story."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/9"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = PatchedClient
    try:
        url = await create_draft_pull_request(
            repo="o/n",
            head_branch="agent/ajob_1",
            base_branch="dev",
            title="t",
            body="b",
            credential=GitHubCredential("secret-token"),
        )
    finally:
        httpx.AsyncClient = original

    assert url == "https://github.com/o/n/pull/9"
    assert captured["body"]["draft"] is True
    assert captured["body"]["head"] == "agent/ajob_1"


async def test_github_error_becomes_a_publish_error():
    """A refused PR is an explicit failure, not a silent success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="head branch does not exist")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = PatchedClient
    try:
        with pytest.raises(PublishError):
            await create_draft_pull_request(
                repo="o/n",
                head_branch="agent/x",
                base_branch="dev",
                title="t",
                body="b",
                credential=GitHubCredential("t"),
            )
    finally:
        httpx.AsyncClient = original


@pytest.mark.parametrize(("field", "retried"), [("head", True), ("base", False)])
async def test_a_head_the_pr_api_cannot_see_yet_is_retried_after_the_push(
    monkeypatch, field, retried
):
    """The draft is opened right after the push, which GitHub may not have seen yet.

    Its ``head invalid`` returns the snapshot to the durable retry budget, with
    the pushed commit still recorded for the retry to build on. A ``base
    invalid`` names the owner's branch, which a retry cannot change, and still
    fails the snapshot.
    """
    store = FakeStore(dict(_JOB))

    def publish(**kwargs):
        kwargs["before_push"]("pushed")
        return PublishResult(branch="agent/athr_1", commit_sha="pushed", changed_files=["x"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert request.method == "POST"
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"resource": "PullRequest", "field": field, "code": "invalid"}],
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(publish_worker, "publish_patch", publish)
    monkeypatch.setattr(httpx, "AsyncClient", PatchedClient)

    assert await publish_one(store, credential=GitHubCredential("t")) is None

    assert store.pushes == [(9, 0, "pushed")]
    assert store.snapshot_recorded is None
    if retried:
        assert store.snapshot_failed is None
        assert store.snapshot_deferred is not None
        assert "head branch" in store.snapshot_deferred[1]
    else:
        assert store.snapshot_deferred is None
        assert store.snapshot_failed is not None
        assert "base branch" in store.snapshot_failed[1]


async def test_credential_never_appears_in_the_pr_body(monkeypatch):
    """The GitHub token must not leak into anything the PR renders."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)
    await publish_one(store, credential=GitHubCredential("super-secret-token"))
    assert "super-secret-token" not in captured["body"]
    assert "super-secret-token" not in captured["title"]


async def test_the_push_credential_never_rides_in_the_clone_url(monkeypatch):
    """In the URL it reached the clone's .git/config and every git-remote-https argv."""
    store = FakeStore(dict(_JOB))
    called: dict[str, Any] = {}

    def fake_publish(**kwargs):
        called.update(kwargs)
        return PublishResult(branch="agent/athr_1", commit_sha="s", changed_files=["x"])

    monkeypatch.setattr(publish_worker, "publish_patch", fake_publish)
    _patch_pr(monkeypatch)
    await publish_one(store, credential=GitHubCredential("super-secret-token"))

    assert called["clone_url"] == "https://github.com/o/n.git"
    assert called["auth_env"] == GitHubCredential("super-secret-token").git_auth_env()


async def test_the_git_credential_is_a_header_only_for_the_git_host(tmp_path):
    """Git sends the token to the repository's host as a header, and nowhere else."""
    import base64
    import subprocess

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        **GitHubCredential("ghs_secret").git_auth_env(),
    }

    def header_for(url: str) -> str:
        return subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", url],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

    basic = base64.b64encode(b"x-access-token:ghs_secret").decode()
    assert header_for("https://github.com/o/n.git") == f"Authorization: Basic {basic}"
    assert header_for("https://elsewhere.example/o/n.git") == ""


@pytest.mark.parametrize(
    ("api_base", "git_base"),
    [
        ("https://api.github.com", "https://github.com"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ],
)
async def test_a_token_goes_back_only_to_the_host_that_minted_it(api_base, git_base):
    """Pushing to github.com whatever the API URL sent another host's token there."""
    credential = GitHubCredential("t", api_base=api_base)

    assert credential.clone_url("o/n") == f"{git_base}/o/n.git"
    assert credential.git_auth_env()["GIT_CONFIG_KEY_0"] == f"http.{git_base}/.extraHeader"


async def test_a_changed_file_name_cannot_break_out_of_its_code_span(monkeypatch):
    """The agent names the files; the App posts the body under its own name.

    Each name stays one code span whose fence no backtick run inside it can
    close, so a crafted name cannot write live mentions, closing keywords or
    links into the body. A control character, which would end the list item
    with the span still open, is shown as an escape rather than written.
    """
    names = [
        "src/plain.py",
        "a` @octocat Fixes #1 [x](https://example.test) `b",
        "c ``` d",
        "`leading",
        "e\n\n@octocat Closes #2",
    ]
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=names)
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"))

    files = captured["body"].split("**Changed files (5)**\n\n", 1)[1].split("\n\n", 1)[0]
    assert files.splitlines() == [
        "- `src/plain.py`",
        "- ``a` @octocat Fixes #1 [x](https://example.test) `b``",
        "- ````c ``` d````",
        "- `` `leading ``",
        "- `e\\n\\n@octocat Closes #2`",
    ]


async def test_app_only_deployment_actually_publishes(monkeypatch):
    """An App-configured deployment with no static token must still publish.

    Exercised through publish_loop, not publish_one: the bug lived in the loop,
    which skipped the entire tick when the static-token provider returned None
    and never passed app_credentials down. The recommended configuration —
    GitHub App, no static token — therefore produced patches forever and never
    a single PR, silently. A test that called publish_one directly would have
    passed against the broken loop.
    """
    import asyncio

    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)

    class FakeApp:
        def __init__(self) -> None:
            self.scoped: bool | None = None

        async def token_for(self, repo: str, **kwargs) -> str:
            self.scoped = kwargs.get("repository_scoped")
            return "ghs_from_app"

    app = FakeApp()

    class LoadedApp:
        """The runtime's answer once the App has loaded, asked once per pass."""

        def get(self) -> FakeApp:
            return app

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=lambda: None,  # no static token configured
            github_app=LoadedApp(),
            interval_seconds=0.01,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if store.snapshot_recorded:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert store.snapshot_recorded == (9, "https://github.com/o/n/pull/7", "s")
    # And the token it published with is scoped to this repository, not to
    # everything the App happens to be installed on.
    assert app.scoped is True


async def test_loop_cancellation_during_heartbeat_cleanup_still_stops_the_publisher(monkeypatch):
    """An owner's stop cannot be mistaken for the heartbeat's expected stop."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)
    heartbeat_started = asyncio.Event()
    heartbeat_joining = asyncio.Event()

    async def heartbeat(*_args, **_kwargs):
        heartbeat_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            heartbeat_joining.set()
            # Hold the join open until the owner is cancelled, so the test
            # exercises the race directly without relying on timer alignment.
            await asyncio.Event().wait()

    async def scoped_credential():
        await heartbeat_started.wait()
        return "ghs_from_app"

    class App:
        async def token_for(self, _repo, **_kwargs):
            return await scoped_credential()

    class LoadedApp:
        def get(self):
            return App()

    monkeypatch.setattr(publish_worker, "_heartbeat_snapshot_publish", heartbeat)
    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=lambda: None,
            github_app=LoadedApp(),
            interval_seconds=0.001,
        )
    )
    try:
        await asyncio.wait_for(heartbeat_joining.wait(), timeout=1.0)
        assert store.snapshot_recorded == (9, "https://github.com/o/n/pull/7", "s")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        assert task.cancelled()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_running_loop_waits_out_an_unloaded_app_and_then_publishes_once(monkeypatch):
    """Issue #47: an App that failed to load is waited for, by the same loop.

    The loop used to be handed the credentials the lifespan had at startup, so
    one failed load meant no PR for the rest of the process. It asks each pass
    now. While the App is unavailable nothing is claimed — the snapshot stays
    queued, not failed — and the static token is not used in its place: a
    deployment that chose an App publishes as the App or not at all.
    """
    import asyncio

    from cloud_agent.source_control_runtime import SourceControlUnavailable

    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    opened: list[dict[str, Any]] = []

    async def fake_pr(**kwargs: Any) -> str:
        opened.append(kwargs)
        return "https://github.com/o/n/pull/7"

    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", fake_pr)

    class LoadedApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            return "ghs_from_app"

    class RecoveringApp:
        """Unavailable for five passes, then loaded — the key fixed in place."""

        def __init__(self) -> None:
            self.asked = 0
            self.claims_while_unavailable: int | None = None

        def get(self) -> LoadedApp:
            self.asked += 1
            if self.asked <= 5:
                raise SourceControlUnavailable("github")
            if self.claims_while_unavailable is None:
                self.claims_while_unavailable = store.claims
            return LoadedApp()

    app = RecoveringApp()
    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=lambda: GitHubCredential("static-token"),
            github_app=app,
            interval_seconds=0.01,
        )
    )
    for _ in range(300):
        await asyncio.sleep(0.01)
        if store.snapshot_recorded and app.asked > 10:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert app.claims_while_unavailable == 0, "nothing may be claimed while the App is down"
    assert store.snapshot_failed is None
    assert store.snapshot_recorded == (9, "https://github.com/o/n/pull/7", "s")
    assert len(opened) == 1, "one PR, however many passes it waited"
    assert opened[0]["credential"].token == "ghs_from_app"


async def test_no_credential_at_all_fails_the_job_rather_than_hanging(monkeypatch):
    """With neither source configured the job says so instead of stalling."""
    store = FakeStore(dict(_JOB))
    assert await publish_one(store, credential=None, app_credentials=None) is None
    assert store.snapshot_failed is not None
    assert "credential" in store.snapshot_failed[1]


async def test_publishing_stops_when_the_entitlement_was_withdrawn(monkeypatch):
    """The write credential must not outlive the permission to use it.

    Publishing happens well after the job ran, and it is the step that asks for
    push authority. Without this the read side would refuse the clone token at
    claim while the same deployment still handed out the push token.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "someone/else")
    store = FakeStore(dict(_JOB))
    minted: list[str] = []

    class App:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            minted.append(repo)
            return "ghs_should_not_happen"

    assert await publish_one(store, app_credentials=App()) is None
    assert minted == [], "no credential may be minted for a repo no longer entitled"
    assert store.snapshot_failed is not None
    assert "entitled" in store.snapshot_failed[1]


async def test_entitlement_outage_defers_before_any_publication_effect(monkeypatch):
    store = FakeStore(dict(_JOB))
    monkeypatch.setattr(
        publish_worker,
        "require_entitled_repo",
        AsyncMock(side_effect=RepoAccessUnavailable("GitHub lookup unavailable")),
    )
    mint = AsyncMock()
    push = Mock()
    pull_request = AsyncMock()
    monkeypatch.setattr(publish_worker, "publish_patch", push)
    monkeypatch.setattr(publish_worker, "ensure_draft_pull_request", pull_request)

    class App:
        token_for = mint

    assert await publish_one(store, app_credentials=App()) is None
    assert store.snapshot_deferred == (
        9,
        "repository access check unavailable; publication will retry: GitHub lookup unavailable",
    )
    assert store.snapshot_failed is None
    assert store.snapshot_recorded is None
    mint.assert_not_awaited()
    push.assert_not_called()
    pull_request.assert_not_awaited()


async def test_entitlement_error_after_publication_started_does_not_requeue(monkeypatch):
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    monkeypatch.setattr(
        publish_worker,
        "ensure_draft_pull_request",
        AsyncMock(side_effect=RepoAccessUnavailable("unavailable after push")),
    )
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_deferred is None
    assert store.snapshot_failed is not None
    assert "unavailable after push" in store.snapshot_failed[1]


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({}, "field absent"),
        ({"html_url": ""}, "empty string"),
        ({"html_url": "   "}, "whitespace"),
        ({"html_url": None}, "null"),
        ({"html_url": 7}, "not a string"),
    ],
)
async def test_a_created_pr_with_no_usable_url_retries_instead_of_recording(
    monkeypatch, payload, why
):
    """**A success this worker cannot record must not be recorded.**

    GitHub answered 2xx, so the PR may well exist — but without a URL there is
    nothing to hand the owner. The old code returned `""` and recorded it, which
    set `published_pr_url` to empty: the job reads as published, and the query
    this worker claims from selects on `published_pr_url IS NULL`, so it is
    never retried. The owner gets no link, ever, and nothing anywhere says so.

    Failing is recoverable — the job is retried and either succeeds or reports
    a reason. Recording an empty URL is not.
    """
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )

    async def _post(*_args, **_kwargs):
        return httpx.Response(201, json=payload)

    async def _get(*_args, **_kwargs):
        return httpx.Response(200, json=[])

    monkeypatch.setattr(publish_worker.httpx.AsyncClient, "get", _get)
    monkeypatch.setattr(publish_worker.httpx.AsyncClient, "post", _post)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert store.snapshot_recorded is None, f"an unusable url was recorded ({why})"
    assert store.snapshot_failed is None, f"the snapshot was terminal ({why})"
    assert store.snapshot_deferred is not None, f"the snapshot was not retried ({why})"
    assert "html_url" in store.snapshot_deferred[1]


@pytest.mark.parametrize(
    "base_ref",
    ["deadbee-release", "abcdef1-release", "release/2026-01", "feature/abc1234"],
)
async def test_a_branch_name_that_merely_starts_like_a_sha_is_kept(monkeypatch, base_ref):
    """A branch is only rejected when the *whole* name is a commit id.

    These already passed before `fullmatch` — the pattern is anchored at both
    ends, so `match` and `fullmatch` agree on every one of them. They are here
    because the property is worth stating, not because they demonstrate the
    change: the only input the two disagree on is one with a trailing newline,
    which `.strip()` removes before either sees it.
    """
    store = FakeStore({**_JOB, "metadata": {"_agent_base_ref": base_ref}})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert captured["base_branch"] == base_ref


async def test_a_base_ref_that_is_only_a_commit_id_falls_back(monkeypatch):
    """The case the check exists for: a commit cannot be a PR base."""
    store = FakeStore({**_JOB, "metadata": {"_agent_base_ref": "abcdef1234567890"}})
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert captured["base_branch"] == "dev"


async def test_environment_decryption_gap_defers_publication_without_remote_side_effects(
    monkeypatch,
):
    from cloud_agent.environments import EnvironmentEncryptionError

    store = FakeStore(dict(_JOB))
    store.get_job_environment = AsyncMock(
        side_effect=EnvironmentEncryptionError("temporary key gap")
    )
    push = Mock()
    monkeypatch.setattr(publish_worker, "publish_patch", push)
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_deferred is not None
    assert store.snapshot_failed is None
    push.assert_not_called()
