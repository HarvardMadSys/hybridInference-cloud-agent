"""Workspace requests fence a stopped group's restore point and wait for a moved one.

A terminal's input, resize and kill cannot start the container, and take no fence.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from cloud_agent import sandbox_manager_routing
from cloud_agent.api._workspace_access import _terminal_control_manager, fence_workspace_start
from cloud_agent.ports.sandbox import TerminalControlRoute


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

    fenced: list[int | None] = []

    class _Manager:
        async def register_shared_sandbox(self, sandbox: dict[str, Any]) -> dict[str, Any]:
            registered.append(sandbox)
            return {}

        def with_start_seq(self, start_seq: int | None) -> Any:
            fenced.append(start_seq)
            return self

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
    # The request itself names it too: the manager labels a run only with
    # the fences of the requests it admitted.
    assert fenced == [27]
    assert job["thread_sandbox"]["start_seq"] == 26


def _route(**changes: Any) -> TerminalControlRoute:
    values: dict[str, Any] = {
        "thread_id": "athr_one",
        "workspace_ready": True,
        "terminal_resume_pending": False,
        "group_state": "running",
        "host_revoked": False,
        "host": {"host": "runner-a", "sandbox_manager_url": "http://runner-a.test:8092"},
    }
    return TerminalControlRoute(**{**values, **changes})


@pytest.mark.parametrize("state", ["running", "stopped"])
def test_a_terminal_control_call_reaches_its_manager_unfenced(monkeypatch, state):
    """Its read names the host, and nothing it sends can start the container."""
    manager = object()
    reached: list[dict[str, Any]] = []

    def client_for(host: dict[str, Any]) -> Any:
        reached.append(host)
        return manager

    monkeypatch.setattr(sandbox_manager_routing, "_client_for", client_for)

    assert _terminal_control_manager(_route(group_state=state)) is manager
    assert reached == [_route().host]


@pytest.mark.parametrize(
    "changes",
    [
        {"group_state": None},
        {"group_state": "cold"},
        {"group_state": "restoring"},
        {"host_revoked": True},
        {"terminal_resume_pending": True},
        {"host": None},
    ],
)
def test_a_terminal_control_call_the_general_path_answers(monkeypatch, changes):
    """No group, one a checkpoint holds or on a removed host, a pending resume, no host.

    Each keeps the answer the fence or the resume gives it.
    """

    def client_for(_host: dict[str, Any]) -> Any:
        raise AssertionError("routed past the general path")

    monkeypatch.setattr(sandbox_manager_routing, "_client_for", client_for)

    assert _terminal_control_manager(_route(**changes)) is None
