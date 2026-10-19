"""Workspace requests fence a stopped group's restore point and wait for a moved one."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from cloud_agent.api._workspace_access import fence_workspace_start


class _Sandbox:
    def __init__(self, state: str | None, start_seq: int | None = 27) -> None:
        self.state = state
        self.start_seq = None if state == "host_revoked" else start_seq
        self.fenced: list[str] = []

    async def fence_workspace_start(self, *, thread_id: str) -> tuple[str, int | None] | None:
        self.fenced.append(thread_id)
        return None if self.state is None else (self.state, self.start_seq)


def _store(state: str | None) -> Any:
    return SimpleNamespace(host_pool=None, sandbox=_Sandbox(state))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stopped", "running", None])
async def test_a_request_fences_the_group_before_it_reaches_the_host(state):
    store = _store(state)

    start_seq = await fence_workspace_start(store, {"thread_id": "athr_one"})

    assert store.sandbox.fenced == ["athr_one"]
    # The request carries the fence it took, not the one its job was read at.
    assert start_seq == (None if state is None else 27)


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


@pytest.mark.asyncio
async def test_the_manifest_names_the_fence_taken_after_the_job_was_read(monkeypatch):
    """Registration tells the manager the new fence, not the job's older one.

    The job is read before its fence, so its manifest names the previous start.
    A manager told only that one labels the run with it, and the checkpoint of
    the run's stop then never covers the group: the group cannot move.
    """
    from cloud_agent.api import _workspace_access

    registered: list[dict[str, Any]] = []

    class _Manager:
        async def register_shared_sandbox(self, sandbox: dict[str, Any]) -> dict[str, Any]:
            registered.append(sandbox)
            return {}

    manager = _Manager()

    async def route(_host_pool: Any, _job_id: str) -> Any:
        return manager

    monkeypatch.setattr(_workspace_access, "sandbox_manager_for_job", route)
    job = {
        "id": "ajob_one",
        "thread_id": "athr_one",
        "thread_sandbox": {"shared_live": True, "state": "stopped", "start_seq": 26},
    }

    assert await _workspace_access._sandbox_manager_for_job(_store("stopped"), job) is manager

    assert [sandbox["start_seq"] for sandbox in registered] == [27]
    assert job["thread_sandbox"]["start_seq"] == 26
