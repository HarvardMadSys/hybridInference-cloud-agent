"""Temporary workspaces through the owner and worker APIs.

A temporary workspace is a conversation created with ``repo: null``: its sandbox
starts from one fixed empty commit, no repository check or credential applies,
and nothing is published. The cases reuse the jobs API's in-memory store and
fixtures, so they exercise the same routes the composer and the runner call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.servers.test_agent_jobs_api import (
    FakeStorage,
    _api_key_secret as _api_key_secret,
    _attach_shared_workspace,
    _build_app,
    _create_job,
    _send,
    client as client,
    store as store,
)

from cloud_agent import sandbox_manager_routing
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent_shared.git.temporary_workspace import TEMPORARY_WORKSPACE_BASE_SHA

_TEMPORARY = {"repo": None, "task_prompt": "write a script", "model": "glm-5.1"}


def _no_repository_check(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Fail the test if any of these modules asks whether a repository is entitled."""

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a temporary workspace has no repository to check")

    for module in modules:
        monkeypatch.setattr(f"{module}.require_entitled_repo", refuse)


async def _create_temporary(client: AsyncClient) -> dict[str, Any]:
    created = await client.post("/v1/agent/jobs", json=_TEMPORARY)
    assert created.status_code == 201, created.text
    return created.json()


async def test_null_repo_creates_a_temporary_workspace_on_the_fixed_base(
    client: AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No repository is checked, and the base is the empty commit every one starts from."""
    _no_repository_check(monkeypatch, "cloud_agent.application.job_admission")

    job = await _create_temporary(client)

    assert job["repo"] is None
    assert job["base_sha"] == TEMPORARY_WORKSPACE_BASE_SHA
    # Nothing is published, so there is no branch to show.
    assert job["output_branch"] is None
    assert store.jobs[job["id"]]["repo"] is None
    fetched = await client.get(f"/v1/agent/jobs/{job['id']}")
    assert fetched.json()["repo"] is None


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"task_prompt": "fix it", "model": "glm-5.1"}, "Field required"),
        # This route reports a body-level refusal without its message, since a
        # request body may carry Secrets; the refusal is what matters here.
        ({**_TEMPORARY, "base_ref": "main"}, "value_error"),
        ({**_TEMPORARY, "base_sha": "a" * 40}, "value_error"),
        ({**_TEMPORARY, "environment_id": "env_1"}, "value_error"),
    ],
)
async def test_a_temporary_workspace_request_names_nothing_in_a_repository(
    client: AsyncClient, store: FakeStorage, body: dict[str, Any], detail: str
) -> None:
    """Leaving repo out is still refused, and repository settings need a repository."""
    refused = await client.post("/v1/agent/jobs", json=body)

    assert refused.status_code == 422
    assert detail in refused.text
    assert store.jobs == {}


async def test_lists_select_temporary_workspaces_or_one_repository(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``temporary`` narrows a list to them; naming a repository as well is refused."""
    repo_job = await _create_job(client)
    temporary = await _create_temporary(client)

    everything = await client.get("/v1/agent/jobs")
    only_temporary = await client.get("/v1/agent/jobs", params={"temporary": "true"})
    contradiction = await client.get(
        "/v1/agent/jobs", params={"temporary": "true", "repo": "owner/name"}
    )

    assert {job["id"] for job in everything.json()["jobs"]} == {repo_job, temporary["id"]}
    assert [job["id"] for job in only_temporary.json()["jobs"]] == [temporary["id"]]
    assert contradiction.status_code == 422
    assert "cannot be combined" in contradiction.text


@pytest.mark.parametrize(
    ("path", "method", "page"),
    [
        (
            "/v1/agent/conversations",
            "list_conversations",
            {"conversations": [], "next_cursor": None},
        ),
        (
            "/v1/agent/sessions",
            "list_sessions",
            {"sessions": [], "total": 0, "projects": [], "next_cursor": None},
        ),
    ],
)
async def test_conversation_and_session_lists_forward_the_temporary_filter(
    client: AsyncClient,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method: str,
    page: dict[str, Any],
) -> None:
    """Both owner lists pass the filter to storage and refuse it beside a repository."""
    listing = AsyncMock(return_value=page)
    monkeypatch.setattr(store, method, listing, raising=False)

    selected = await client.get(path, params={"temporary": "true"})
    contradiction = await client.get(path, params={"temporary": "true", "repo": "owner/name"})

    assert selected.status_code == 200, selected.text
    assert listing.await_args.kwargs["temporary"] is True
    assert listing.await_args.kwargs["repo"] is None
    assert contradiction.status_code == 422
    assert listing.await_count == 1


async def test_a_follow_up_in_a_temporary_workspace_checks_no_repository(
    client: AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next message is admitted on the owner's access alone, keeping the base."""
    job = await _create_temporary(client)
    _no_repository_check(monkeypatch, "cloud_agent.application.follow_ups")
    store.jobs[job["id"]]["state"] = "succeeded"

    sent = await _send(client, store, job["id"], {"prompt": "now add tests"})

    assert sent.status_code == 201, sent.text
    follow_up = sent.json()["job"]
    assert follow_up["repo"] is None
    assert follow_up["base_sha"] == TEMPORARY_WORKSPACE_BASE_SHA


async def test_the_claim_carries_no_repository_credential(
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no repository there is nothing to clone and nothing to scope a token to."""
    from cloud_agent.api.deps import get_agent_app_credentials

    asked: list[str] = []

    class FakeApp:
        async def token_for(self, repo: str, **_kwargs: Any) -> str:
            asked.append(repo)
            return "token"

    _no_repository_check(monkeypatch, "cloud_agent.api.worker")
    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FakeApp()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _create_temporary(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 200, claim.text
    body = claim.json()
    assert body["repo"] is None
    assert body["base_sha"] == TEMPORARY_WORKSPACE_BASE_SHA
    assert body["clone_token"] is None
    assert body["github_token"] is None
    assert asked == []


async def test_a_temporary_claim_receives_only_its_owners_durable_memories(
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker reads user memory with no repository and injects its fenced tool ids."""
    read = AsyncMock(
        return_value={
            "repo": None,
            "project": [],
            "user": [{"id": "user-rule", "content": "Always label chart axes."}],
        }
    )
    mark = AsyncMock()
    monkeypatch.setattr(store, "task_memories", read, raising=False)
    monkeypatch.setattr(store, "mark_memories_used", mark, raising=False)
    _no_repository_check(monkeypatch, "cloud_agent.api.worker")
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create_temporary(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 200, claim.text
    owner = store.jobs[job["id"]]["user_id"]
    read.assert_awaited_once_with(owner, None)
    mark.assert_awaited_once_with(owner, ["user-rule"])
    prompt = claim.json()["task_prompt"]
    assert '"project": {"repo": null, "memories": []}' in prompt
    assert '{"id": "user-rule", "content": "Always label chart axes."}' in prompt
    assert "memory_update" in prompt
    assert prompt.endswith("Current task:\nwrite a script")


class _ArchiveStream:
    """What ``SandboxManagerClient.workspace_archive`` hands back, in memory."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.content_length = sum(len(chunk) for chunk in chunks)
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _SandboxManager:
    def __init__(self, archive: _ArchiveStream | SandboxManagerError) -> None:
        self.archive = archive
        self.calls: list[tuple[str, str]] = []

    def with_start_seq(self, _start_seq: int | None):
        return self

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]) -> None:
        return None

    async def files(self, workspace_id: str, path: str, *, profiling: Any = None) -> dict:
        self.calls.append(("files", workspace_id))
        return {"path": path, "kind": "directory", "entries": [], "writable": True}

    async def workspace_archive(self, workspace_id: str) -> _ArchiveStream:
        self.calls.append(("archive", workspace_id))
        if isinstance(self.archive, SandboxManagerError):
            raise self.archive
        return self.archive


async def test_the_workspace_downloads_as_one_archive_from_its_host(
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner gets the host's archive as an attachment, named after the conversation."""
    stream = _ArchiveStream([b"\x1f\x8b", b"archive-bytes"])
    manager = _SandboxManager(stream)
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: manager)
    _no_repository_check(monkeypatch, "cloud_agent.api.workspace")
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create_temporary(client)
        store.jobs[job["id"]]["title"] = "数据/分析"
        workspace_id = _attach_shared_workspace(store, job["id"])
        listing = await client.get(f"/v1/agent/jobs/{job['id']}/files")
        download = await client.get(f"/v1/agent/jobs/{job['id']}/workspace/archive")

    assert listing.status_code == 200, listing.text
    assert download.status_code == 200, download.text
    assert download.content == b"\x1f\x8barchive-bytes"
    assert download.headers["content-type"] == "application/gzip"
    assert download.headers["content-length"] == str(stream.content_length)
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'filename="workspace-{store.jobs[job["id"]]["thread_id"]}.tar.gz"' in disposition
    assert "filename*=UTF-8''%E6%95%B0%E6%8D%AE-%E5%88%86%E6%9E%90.tar.gz" in disposition
    assert download.headers["cache-control"] == "no-store"
    assert manager.calls == [("files", workspace_id), ("archive", workspace_id)]
    assert stream.closed


async def test_a_workspace_over_the_hosts_download_limit_is_refused_whole(
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's 413 reaches the owner with its message, before any byte is sent."""
    manager = _SandboxManager(
        SandboxManagerError(
            413, "The workspace is larger than this server's 1024 MiB download limit."
        )
    )
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create_temporary(client)
        _attach_shared_workspace(store, job["id"])
        refused = await client.get(f"/v1/agent/jobs/{job['id']}/workspace/archive")

    assert refused.status_code == 413
    assert "1024 MiB download limit" in refused.json()["detail"]["error"]["message"]


async def test_a_download_needs_an_owned_retained_workspace(
    client: AsyncClient, store: FakeStorage
) -> None:
    """No sandbox yet is a conflict, and someone else's conversation does not exist."""
    job = await _create_temporary(client)

    not_yet = await client.get(f"/v1/agent/jobs/{job['id']}/workspace/archive")
    missing = await client.get("/v1/agent/jobs/ajob_missing/workspace/archive")

    assert not_yet.status_code == 409
    assert not_yet.json()["detail"]["error"]["type"] == "workspace_unavailable"
    assert missing.status_code == 404
