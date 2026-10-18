"""Workspace requests fence a stopped group's restore point and wait for a moved one."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from cloud_agent.api._workspace_access import fence_workspace_start


class _Sandbox:
    def __init__(self, state: str | None) -> None:
        self.state = state
        self.fenced: list[str] = []

    async def fence_workspace_start(self, *, thread_id: str) -> str | None:
        self.fenced.append(thread_id)
        return self.state


def _store(state: str | None) -> Any:
    return SimpleNamespace(host_pool=None, sandbox=_Sandbox(state))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stopped", "running", None])
async def test_a_request_fences_the_group_before_it_reaches_the_host(state):
    store = _store(state)

    await fence_workspace_start(store, {"thread_id": "athr_one"})

    assert store.sandbox.fenced == ["athr_one"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["cold", "restoring"])
async def test_a_moved_group_answers_that_a_message_restores_it(state):
    with pytest.raises(HTTPException) as refused:
        await fence_workspace_start(_store(state), {"thread_id": "athr_one"})

    assert refused.value.status_code == 409
    assert refused.value.detail["error"]["type"] == "workspace_restoring"


@pytest.mark.asyncio
async def test_a_job_without_a_conversation_fences_nothing():
    store = _store("cold")

    await fence_workspace_start(store, {"thread_id": None})

    assert store.sandbox.fenced == []


@pytest.mark.asyncio
async def test_a_group_on_a_removed_host_is_refused():
    with pytest.raises(HTTPException) as refused:
        await fence_workspace_start(_store("host_revoked"), {"thread_id": "athr_one"})

    assert refused.value.status_code == 409
    error = refused.value.detail["error"]
    assert error["type"] == "workspace_unavailable"
    assert "server that has been removed" in error["message"]
