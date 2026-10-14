"""Durable memories are read and changed only by their authenticated owner."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.agent_memory import AgentMemoryConflict, AgentMemoryFull, AgentMemoryNotFound
from cloud_agent.api import agent_memory
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import install_admin_no_store

PATH = "/v1/agent/memories"


def memory(**changes):
    return {
        "id": "m1",
        "repo": None,
        "content": "Reply in Chinese",
        "description": "Reply in Chinese",
        "status": "active",
        "origin": "manual",
        "source_job_ids": [],
        "version": 0,
        "created_at": "2026-10-13T00:00:00Z",
        "updated_at": "2026-10-13T00:00:00Z",
        **changes,
    }


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "memory-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(agent_memory.router)
    install_admin_no_store(application, prefixes=(PATH,))
    repository = AsyncMock()
    repository.list_memories.side_effect = lambda user_id, repo: {
        "repo": repo,
        "enabled": True,
        "memories": [memory(repo=repo)],
        "archived": [],
    }
    repository.add_memory.side_effect = lambda user_id, repo, content: memory(
        repo=repo, content=content
    )
    repository.update_memory.side_effect = lambda user_id, memory_id, **kw: memory(
        id=memory_id, repo=kw["repo"], content=kw["content"], version=1
    )
    repository.restore_memory.side_effect = lambda user_id, memory_id, **kw: memory(id=memory_id)
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(agent_memory=repository)
    application.state.repository = repository
    return application


def headers(user_id="owner-1"):
    return {"Authorization": f"Bearer {identity.mint_session(user_id=user_id)}"}


def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    )


async def test_reads_are_owner_scoped_and_never_cached(app):
    async with client(app) as http:
        user = await http.get(PATH + "?user_id=owner-2", headers=headers())
        project = await http.get(PATH, params={"repo": "owner/api"}, headers=headers())
        anonymous = await http.get(PATH)
    assert user.status_code == project.status_code == 200
    assert user.json()["repo"] is None and project.json()["repo"] == "owner/api"
    assert user.headers["cache-control"] == "no-store"
    assert anonymous.status_code == 401
    calls = [call.args for call in app.state.repository.list_memories.await_args_list]
    assert calls == [("owner-1", None), ("owner-1", "owner/api")]


async def test_owner_writes_reach_the_repository_with_normalized_content(app):
    repository = app.state.repository
    async with client(app) as http:
        added = await http.post(
            PATH, json={"content": "  以后都用中文回复  ", "repo": None}, headers=headers()
        )
        moved = await http.patch(
            PATH + "/m1",
            json={"content": "Use pnpm", "repo": "owner/api", "expected_version": 0},
            headers=headers(),
        )
        deleted = await http.request(
            "DELETE", PATH + "/m1", json={"expected_version": 1}, headers=headers()
        )
        restored = await http.post(
            PATH + "/m1/restore", json={"expected_version": 2}, headers=headers()
        )
        cleared = await http.delete(PATH, params={"repo": "owner/api"}, headers=headers())
    assert added.status_code == 201 and added.json()["content"] == "以后都用中文回复"
    assert moved.status_code == 200 and moved.json()["repo"] == "owner/api"
    assert deleted.status_code == cleared.status_code == 204
    assert restored.status_code == 200
    repository.add_memory.assert_awaited_once_with("owner-1", None, "以后都用中文回复")
    assert repository.update_memory.await_args.kwargs == {
        "content": "Use pnpm",
        "repo": "owner/api",
        "expected_version": 0,
    }
    repository.delete_memory.assert_awaited_once_with("owner-1", "m1", expected_version=1)
    repository.restore_memory.assert_awaited_once_with("owner-1", "m1", expected_version=2)
    repository.clear_memories.assert_awaited_once_with("owner-1", "owner/api")


@pytest.mark.parametrize(
    "body",
    [
        {"content": "Use token=privatevalue", "repo": None},
        {"content": "Skip tests for this PR", "repo": None},
        {"content": "x" * 241, "repo": None},
        {"content": "Use pnpm"},
        {"content": "Use pnpm", "repo": " "},
        {"content": "Use pnpm", "repo": None, "extra": True},
    ],
)
async def test_invalid_memories_are_rejected_without_echoing_them(app, body):
    async with client(app) as http:
        response = await http.post(PATH, json=body, headers=headers())
    assert response.status_code == 422
    assert "privatevalue" not in response.text and "PR" not in response.text
    app.state.repository.add_memory.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AgentMemoryNotFound("secret text"), 404),
        (AgentMemoryConflict("secret text"), 409),
        (AgentMemoryFull("secret text"), 409),
    ],
)
async def test_repository_failures_use_fixed_answers(app, error, status):
    app.state.repository.update_memory.side_effect = error
    async with client(app) as http:
        response = await http.patch(
            PATH + "/m1",
            json={"content": "Use pnpm", "repo": None, "expected_version": 0},
            headers=headers(),
        )
    assert response.status_code == status
    assert "secret" not in response.text
