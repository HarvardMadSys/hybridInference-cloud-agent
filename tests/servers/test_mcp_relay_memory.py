"""The merged MCP endpoint serves the platform's memory tools for the fenced task."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.servers.test_mcp_relay_host import (
    _ATTEMPT,
    _HOST_URL,
    _JOB,
    _LEASE,
    _OWNER,
    FakeStore,
    _auth,
    _result,
    _rpc,
    _secrets as _secrets,
)
from tests.support.storage import OneFake

from cloud_agent import memory_tool
from cloud_agent.agent_memory import MemoryTask
from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_storage


class MemoryStore(FakeStore):
    """The relay's fake store, recording memory writes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.saved: list[tuple[Any, ...]] = []

    async def save_agent_memory(
        self, task: MemoryTask, content: str, *, all_projects: bool
    ) -> dict[str, Any]:
        self.saved.append((task, content, all_projects))
        return {"id": "m1", "repo": None if all_projects else "owner/api", "content": content}


@pytest.fixture()
def memory_store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture()
async def api(memory_store: MemoryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(memory_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_the_memory_tools_are_listed_with_their_schemas(api: AsyncClient) -> None:
    response = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    listed = {tool["name"]: tool for tool in _result(response)["tools"]}
    for definition in memory_tool.TOOL_DEFINITIONS:
        assert listed[definition["name"]] == definition


async def test_a_save_acts_for_the_task_the_token_names(
    api: AsyncClient, memory_store: MemoryStore
) -> None:
    response = await api.post(
        _HOST_URL,
        json=_rpc(
            "tools/call",
            name=memory_tool.SAVE,
            # Arguments cannot choose whose memory this is.
            arguments={"content": "Use pnpm, not npm.", "user_id": "mallory", "job_id": "other"},
        ),
        headers=_auth(),
    )
    result = _result(response)
    # The attempt goes along, so the write re-checks it in its own transaction.
    task = MemoryTask(user_id=_OWNER, job_id=_JOB, attempt_id=_ATTEMPT, lease_generation=_LEASE)
    assert memory_store.saved == [(task, "Use pnpm, not npm.", False)]
    assert json.loads(result["content"][0]["text"])["saved"]["id"] == "m1"


async def test_a_stale_token_cannot_write_memory(
    api: AsyncClient, memory_store: MemoryStore
) -> None:
    memory_store.live = False
    response = await api.post(
        _HOST_URL,
        json=_rpc("tools/call", name=memory_tool.SAVE, arguments={"content": "Use pnpm."}),
        headers=_auth(),
    )
    assert response.status_code == 401
    assert memory_store.saved == []
