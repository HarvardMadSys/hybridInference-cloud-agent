"""Both memory scopes stay isolated and neither can block claim execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.servers.test_agent_jobs_api import (
    _OWNER,
    _api_key_secret as _api_key_secret,
    _create_job,
    client as client,
    store as store,
)

from cloud_agent.api import worker


def user_view(*, enabled=True):
    return {
        "enabled": enabled,
        "memories": [
            {
                "kind": "preference",
                "scope": "user",
                "content": "I prefer concise summaries.",
                "source_repo": None,
            },
            {"kind": "task", "scope": "user", "content": "A prior unrelated task"},
            {
                "kind": "preference",
                "scope": "repo",
                "content": "I prefer verbose database migration reports.",
                "source_repo": "another/project",
            },
        ],
    }


def project_view(*, enabled=True, repo="owner/name"):
    return {
        "enabled": enabled,
        "repo": repo,
        "memories": [
            {
                "kind": "preference",
                "scope": "repo",
                "content": "Always run pytest before reporting changes.",
                "source_repo": repo,
            },
            {
                "kind": "task",
                "scope": "repo",
                "content": "A prior project task",
                "source_repo": repo,
            },
            {
                "kind": "preference",
                "scope": "repo",
                "content": "I prefer changing unrelated repository settings.",
                "source_repo": "another/project",
            },
        ],
    }


@pytest.mark.parametrize("user_enabled", [True, False])
@pytest.mark.parametrize("project_enabled", [True, False])
async def test_claim_uses_only_enabled_scopes_for_the_job_owner_and_repo(
    client, store, user_enabled, project_enabled
):
    job_id = await _create_job(client)
    refresh = AsyncMock(return_value=user_view(enabled=user_enabled))
    refresh_project = AsyncMock(return_value=project_view(enabled=project_enabled))
    store.user_memory = SimpleNamespace(refresh=refresh, refresh_project=refresh_project)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == job_id
    prompt = claim.json()["task_prompt"]
    assert ("I prefer concise summaries." in prompt) is user_enabled
    assert ("Always run pytest before reporting changes." in prompt) is project_enabled
    assert "prior unrelated task" not in prompt
    assert "prior project task" not in prompt
    assert "verbose database" not in prompt
    assert "changing unrelated repository settings" not in prompt
    assert "The current task always takes precedence." in prompt or not (
        user_enabled or project_enabled
    )
    assert prompt.endswith("fix it")
    assert store.jobs[job_id]["task_prompt"] == "fix it"
    refresh.assert_awaited_once_with(user_id=_OWNER)
    refresh_project.assert_awaited_once_with(user_id=_OWNER, repo="owner/name")


async def test_claim_still_starts_when_memory_temporarily_fails(client, store):
    job_id = await _create_job(client)
    store.user_memory = SimpleNamespace(
        refresh=AsyncMock(side_effect=RuntimeError("unavailable")),
        refresh_project=AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == job_id
    assert claim.json()["task_prompt"] == "fix it"


@pytest.mark.parametrize("failed_scope", ["user", "project"])
async def test_failure_in_one_scope_preserves_the_other(client, store, failed_scope):
    await _create_job(client)
    refresh = AsyncMock(return_value=user_view())
    refresh_project = AsyncMock(return_value=project_view())
    failed = refresh if failed_scope == "user" else refresh_project
    failed.side_effect = RuntimeError("unavailable")
    store.user_memory = SimpleNamespace(refresh=refresh, refresh_project=refresh_project)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    prompt = claim.json()["task_prompt"]
    assert ("I prefer concise summaries." in prompt) is (failed_scope != "user")
    assert ("Always run pytest before reporting changes." in prompt) is (failed_scope != "project")
    assert prompt.endswith("fix it")


async def test_claim_rejects_project_memory_for_another_repository(client, store):
    await _create_job(client)
    store.user_memory = SimpleNamespace(
        refresh=AsyncMock(return_value=user_view()),
        refresh_project=AsyncMock(return_value=project_view(repo="another/project")),
    )
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    prompt = claim.json()["task_prompt"]
    assert "I prefer concise summaries." in prompt
    assert "Always run pytest before reporting changes." not in prompt
    assert "prior project task" not in prompt


@pytest.mark.parametrize("slow_scope", ["user", "project", "both"])
async def test_slow_memory_is_cancelled_without_losing_other_scope(
    client, store, monkeypatch, slow_scope
):
    job_id = await _create_job(client)
    cancelled = asyncio.Event()

    async def refresh(**_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(worker, "_USER_MEMORY_TIMEOUT_SECONDS", 0.01)
    store.user_memory = SimpleNamespace(
        refresh=refresh if slow_scope in ("user", "both") else AsyncMock(return_value=user_view()),
        refresh_project=refresh
        if slow_scope in ("project", "both")
        else AsyncMock(return_value=project_view()),
    )
    claim = await asyncio.wait_for(
        client.post("/v1/agent/worker/claim", json={"worker_id": "w1"}), timeout=1
    )
    assert cancelled.is_set()
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == job_id
    prompt = claim.json()["task_prompt"]
    assert ("I prefer concise summaries." in prompt) is (slow_scope == "project")
    assert ("Always run pytest before reporting changes." in prompt) is (slow_scope == "user")
    assert prompt.endswith("fix it")
