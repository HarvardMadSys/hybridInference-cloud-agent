"""The sandbox manager's workspace routes stand on their admission's network proof.

Opening a read or a terminal, or an environment preparation that runs a
script, is admitted by the supervisor, which proves the group network for that
request. No route proves it again, and the commands an operation spawns stand
on that proof. A preparation already done for the running boot runs nothing
and proves nothing.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.test_agent_sandbox_manager import _app, _SharedSupervisor


@pytest.mark.asyncio
async def test_workspace_routes_leave_the_network_proof_to_admission(monkeypatch):
    """The admission that opens a read, a terminal or a preparation proves the network.

    No route proves it a second time, and a command an operation spawns
    stands on its admission's proof rather than making another.
    """
    supervisor = _SharedSupervisor()
    proofs: list[object] = []
    supervisor.workspace_backend.manager.require_network = lambda spec, **_kwargs: proofs.append(
        spec
    )
    prepared: list[str] = []

    def prepare_environment(thread_id, _environment, _preparer, **_kwargs):
        prepared.append(thread_id)
        return {"ready": True}

    supervisor.prepare_environment = prepare_environment  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "cloud_agent_host.sandbox_manager.read_retained_file",
        lambda *_args, **_kwargs: {"path": "", "kind": "directory", "entries": []},
    )
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    before = time.monotonic()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        listed = await client.get("/workspaces/athr_source/files?path=", headers=headers)
        proved_from = supervisor.network_proved_at
        created = await client.post(
            "/workspaces/athr_source/terminals", headers=headers, json={"rows": 30, "cols": 100}
        )
        environment = await client.post(
            "/workspaces/athr_source/environment/prepare",
            headers=headers,
            json={"environment": {"binding_id": "binding"}},
        )
        deleted = await client.delete(
            f"/workspaces/athr_source/terminals/{created.json()['id']}", headers=headers
        )

    assert listed.status_code == created.status_code == environment.status_code == 200
    assert deleted.status_code == 200
    assert proofs == []
    assert ("operation_opened", "athr_source") in supervisor.calls
    assert ("terminal_opened", "athr_source") in supervisor.calls
    assert prepared == ["athr_source"]
    assert proved_from is not None and before <= proved_from <= time.monotonic()
