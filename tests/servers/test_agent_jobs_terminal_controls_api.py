"""Terminal controls for the job router: keystrokes, resizes and kills.

A control cannot start the container, so it takes neither the start fence nor
a manifest registration, and a refusal from a manager that lost the chat is
repaired once. These cases reuse the route suite's in-memory store.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    FakeStorage,
    _api_key_secret,
    _attach_shared_workspace,
    _build_app,
    _configured_identity_issuer,
    _create_job,
    _TerminalSessionSandboxManager,
    store,
)

from cloud_agent import sandbox_manager_routing
from cloud_agent.api import workspace as agent_workspace

pytestmark = pytest.mark.asyncio


async def test_terminal_controls_use_the_attached_session_capability(
    store: FakeStorage, monkeypatch
):
    """Attached PTY controls skip entitlement and never replay the event log."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)

    async def unexpected_entitlement_recheck(**_kwargs):
        raise AssertionError("attached terminal controls must not call external entitlement")

    monkeypatch.setattr(
        agent_workspace,
        "_require_workspace_entitlement",
        unexpected_entitlement_recheck,
    )

    async def unexpected_event_query(**_kwargs):
        raise AssertionError("terminal input must use the attempt readiness field")

    monkeypatch.setattr(store, "list_events_after", unexpected_event_query)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        claimed = await store.attempt_commands.claim_job(
            worker_id="worker-1",
            lease_ttl_seconds=60,
            host="runner-test",
            host_credential_hash="test-credential-hash",
        )
        assert claimed is not None
        await store.attempt_commands.append_event(
            attempt_id=claimed["attempt_id"],
            lease_generation=claimed["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        wrote = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        resized = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        )
        killed = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    assert wrote.status_code == 200
    assert resized.status_code == 200
    assert killed.status_code == 200
    assert sandbox_manager.calls == [
        ("input", workspace_id, "term_1", "eA=="),
        ("resize", workspace_id, "term_1", 30, 100),
        ("delete", workspace_id, "term_1"),
    ]


def _registering(sandbox_manager: _TerminalSessionSandboxManager) -> list[str]:
    """Record each manifest registration among the manager's calls."""
    registered: list[str] = []

    async def register_shared_sandbox(sandbox: dict[str, Any]) -> None:
        registered.append(sandbox["thread_id"])
        sandbox_manager.calls.append(("register", sandbox["thread_id"]))

    sandbox_manager.register_shared_sandbox = register_shared_sandbox  # type: ignore[method-assign]
    return registered


async def test_terminal_keystrokes_neither_fence_the_group_nor_register_it(
    store: FakeStorage, monkeypatch
):
    """Input, a resize and a kill cannot start the container, so none takes the start fence.

    The browser sends about one input per keystroke. Each used to write the
    group's row for the fence and register the chat's manifest with the host,
    a second trip over its tunnel, before the input itself. Opening, listing
    and attaching a terminal may start the container, and still do both.
    """
    sandbox_manager = _TerminalSessionSandboxManager()
    registered = _registering(sandbox_manager)
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["thread_sandbox"]["state"] = "stopped"
        store.jobs[job_id]["state"] = "succeeded"
        terminal = f"/v1/agent/jobs/{job_id}/terminals/term_1"
        controls = [
            await client.post(f"{terminal}/input", json={"data": "bHMK"}),
            await client.post(f"{terminal}/resize", json={"rows": 30, "cols": 100}),
            await client.delete(terminal),
        ]
        fenced_by_controls = list(store.workspace_start_fences)
        registered_by_controls = list(registered)
        opened = [
            await client.post(f"/v1/agent/jobs/{job_id}/terminals", json={"rows": 24, "cols": 80}),
            await client.get(f"/v1/agent/jobs/{job_id}/terminals"),
            await client.get(f"{terminal}/stream"),
        ]

    assert [response.status_code for response in controls + opened] == [200] * 6
    assert fenced_by_controls == []
    assert registered_by_controls == []
    assert store.workspace_start_fences == [workspace_id] * 3
    assert sandbox_manager.calls == [
        ("input", workspace_id, "term_1", "bHMK"),
        ("resize", workspace_id, "term_1", 30, 100),
        ("delete", workspace_id, "term_1"),
        ("register", workspace_id),
        ("create", workspace_id, 24, 80),
        ("register", workspace_id),
        ("list", workspace_id),
        ("register", workspace_id),
        ("stream", workspace_id, "term_1", 0),
    ]


@pytest.mark.parametrize("state", ["cold", "restoring", "host_revoked"])
async def test_terminal_keystrokes_to_a_moved_or_stranded_group_keep_their_conflicts(
    store: FakeStorage, monkeypatch, state: str
):
    """A group a checkpoint holds, or one on a removed host, answers as it always has.

    Not the manager's 404 for a terminal it does not hold: the 409 that says a
    message brings the files back, or that their server was removed.
    """
    from cloud_agent.api._workspace_access import _workspace_host_removed, _workspace_restoring

    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["thread_sandbox"]["state"] = state
        store.jobs[job_id]["state"] = "succeeded"
        terminal = f"/v1/agent/jobs/{job_id}/terminals/term_1"
        responses = [
            await client.post(f"{terminal}/input", json={"data": "bHMK"}),
            await client.post(f"{terminal}/resize", json={"rows": 30, "cols": 100}),
            await client.delete(terminal),
        ]

    expected = _workspace_host_removed() if state == "host_revoked" else _workspace_restoring()
    for response in responses:
        assert response.status_code == 409
        assert response.json()["detail"] == expected.detail
    assert sandbox_manager.calls == []


async def test_a_keystroke_registers_a_chat_its_restarted_manager_forgot_and_is_sent_again(
    store: FakeStorage, monkeypatch
):
    """A restarted manager has lost the forked chats' aliases with its terminals.

    It refuses the keystroke as a workspace it does not hold, before touching
    any terminal. The keystroke then takes the general path, which registers
    the chat's manifest again, and is sent once more: its answer is the one a
    keystroke got when each registered first. The next keystroke goes direct.
    """
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    sandbox_manager = _TerminalSessionSandboxManager()
    registered = _registering(sandbox_manager)
    write = sandbox_manager.terminal_input

    async def terminal_input(workspace_id: str, terminal_id: str, *, data: str):
        if not registered:
            sandbox_manager.calls.append(("refused", workspace_id))
            raise SandboxManagerError(404, "shared chat workspace is not materialized")
        return await write(workspace_id, terminal_id, data=data)

    monkeypatch.setattr(sandbox_manager, "terminal_input", terminal_input)
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "succeeded"
        terminal = f"/v1/agent/jobs/{job_id}/terminals/term_1"
        first = await client.post(f"{terminal}/input", json={"data": "bHMK"})
        second = await client.post(f"{terminal}/input", json={"data": "cHdkCg=="})

    assert (first.status_code, second.status_code) == (200, 200)
    assert sandbox_manager.calls == [
        ("refused", workspace_id),
        ("register", workspace_id),
        ("input", workspace_id, "term_1", "bHMK"),
        ("input", workspace_id, "term_1", "cHdkCg=="),
    ]
    assert store.workspace_start_fences == [workspace_id]


async def test_a_keystroke_to_a_workspace_its_manager_still_lacks_is_a_conflict(
    store: FakeStorage, monkeypatch
):
    """Registered again and still unknown there, the chat has no live terminal workspace."""
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    sandbox_manager = _TerminalSessionSandboxManager()
    _registering(sandbox_manager)

    async def terminal_input(workspace_id: str, terminal_id: str, *, data: str):
        sandbox_manager.calls.append(("refused", workspace_id))
        raise SandboxManagerError(404, "shared chat workspace is not materialized")

    monkeypatch.setattr(sandbox_manager, "terminal_input", terminal_input)
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        workspace_id = _attach_shared_workspace(store, job_id)
        response = await client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input", json={"data": "bHMK"}
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": {
            "type": "workspace_unavailable",
            "message": "This chat has no live terminal workspace.",
        }
    }
    assert sandbox_manager.calls == [
        ("refused", workspace_id),
        ("register", workspace_id),
        ("refused", workspace_id),
    ]


async def test_a_keystroke_first_resumes_a_settled_turns_terminals(store: FakeStorage, monkeypatch):
    """A resume the settling turn could not confirm is repaired by the next keystroke too."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "cancelled"
        store.jobs[job_id]["terminal_resume_pending"] = True
        first = await client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input", json={"data": "bHMK"}
        )
        second = await client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input", json={"data": "bHMK"}
        )

    assert (first.status_code, second.status_code) == (200, 200)
    assert sandbox_manager.calls == [
        ("resume_settled", workspace_id, job_id),
        ("input", workspace_id, "term_1", "bHMK"),
        ("input", workspace_id, "term_1", "bHMK"),
    ]
    assert store.jobs[job_id]["terminal_resume_pending"] is False
    assert store.workspace_start_fences == [workspace_id]
