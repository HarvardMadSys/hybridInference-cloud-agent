"""Unit tests for the publish step (patch → branch → draft PR).

This is the hop that produces the visible product outcome, and the one where a
mistake is externally visible, so the cases below are the ones that would show
up in someone's repository: double-publishing, publishing a blocked patch, and
opening a PR that is not a draft.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest

from cloud_agent import publish_worker
from cloud_agent.publish_worker import (
    GitHubCredential,
    create_draft_pull_request,
    publish_loop,
    publish_one,
)
from cloud_agent.publisher import PublishError, PublishResult

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
    "patch": "diff --git a/x b/x\n",
}


class FakeStore:
    """Records what the publisher did to the job."""

    def __init__(self, job: dict[str, Any] | None = None, *, record_ok: bool = True) -> None:
        self._job = job
        self.claims = 0
        self.snapshot_recorded: tuple[int, str, str] | None = None
        self.snapshot_failed: tuple[int, str] | None = None
        self.import_failed: tuple[int, str] | None = None
        self.import_completed: tuple[int, str] | None = None
        self._record_ok = record_ok

    async def claim_for_publish(self):
        self.claims += 1
        job, self._job = self._job, None
        return job

    async def record_snapshot_publish(
        self, *, snapshot_id: int, pr_url: str, commit_sha: str
    ) -> bool:
        self.snapshot_recorded = (snapshot_id, pr_url, commit_sha)
        return self._record_ok

    async def fail_snapshot_publish(self, *, snapshot_id: int, detail: str) -> None:
        self.snapshot_failed = (snapshot_id, detail)

    async def record_snapshot_base_import_failure(self, *, snapshot_id: int, detail: str) -> None:
        self.import_failed = (snapshot_id, detail)

    async def complete_snapshot_base_import(self, *, snapshot_id: int, commit_sha: str) -> bool:
        self.import_completed = (snapshot_id, commit_sha)
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

    monkeypatch.setattr(publish_worker, "create_draft_pull_request", fake)
    return captured


async def test_empty_queue_is_a_no_op():
    """Nothing pending means nothing happens."""
    store = FakeStore(None)
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.snapshot_recorded is None and store.snapshot_failed is None


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
    assert "ajob_1" in captured["body"]


async def test_shared_snapshot_publishes_group_lineage_and_imports_the_new_base(monkeypatch):
    from cloud_agent import broker_routing

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

    class Broker:
        def __init__(self) -> None:
            self.imported: tuple[dict[str, Any], str, bytes] | None = None

        async def import_shared_base(self, row, *, commit_sha: str, bundle: bytes):
            self.imported = (row, commit_sha, bundle)

    broker = Broker()

    async def route(_store, job_id: str):
        assert job_id == "ajob_2"
        return broker

    monkeypatch.setattr(broker_routing, "broker_for_job", route)

    url = await publish_one(store, credential=GitHubCredential("t"))

    assert url == "https://github.com/o/n/pull/7"
    assert store.snapshot_recorded == (9, url, "b" * 40)
    assert store.snapshot_failed is None and store.import_failed is None
    assert store.import_completed == (9, "b" * 40)
    assert broker.imported == (snapshot, "b" * 40, b"bundle")
    assert "group snapshot" in captured_pr["body"]
    assert "ajob_1" in captured_pr["body"] and "ajob_2" in captured_pr["body"]


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
    """One conversation owns one branch and one draft PR across its turns."""
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
    assert pr_called == {}
    assert store.snapshot_recorded == (9, url, "next-sha")


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

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=lambda: None,  # no static token configured
            app_credentials=app,
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
async def test_a_created_pr_with_no_usable_url_fails_instead_of_recording(
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

    monkeypatch.setattr(publish_worker.httpx.AsyncClient, "post", _post)

    await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")

    assert store.snapshot_recorded is None, f"an unusable url was recorded ({why})"
    assert store.snapshot_failed is not None, f"the snapshot was not failed ({why})"
    assert "html_url" in store.snapshot_failed[1]


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
