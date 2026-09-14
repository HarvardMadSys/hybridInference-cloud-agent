"""A slow connection test cannot publish stale results or leak its transport."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from cloud_agent.api import mcp_connections
from cloud_agent.mcp import host, protocol
from cloud_agent.mcp.models import CapabilitySnapshot
from tests.unit.test_mcp_connections_api import FakeStore
from tests.unit.test_mcp_host_sessions_and_results import Transport

pytestmark = pytest.mark.asyncio


async def setup_diagnostic(monkeypatch, *, fail=False):
    """Open a real host session and pause only its upstream discovery reply."""
    store = FakeStore()
    row = await store.create_mcp_connection(
        user_id="owner",
        label="Docs",
        transport="streamable-http",
        location="remote",
        url="https://docs.example.com/mcp",
        auth_kind="none",
    )
    built = host.McpHost()
    transport = Transport()
    monkeypatch.setattr(
        built,
        "_connect",
        AsyncMock(return_value=(transport, protocol.Handshake("2025-11-25"), None)),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def discover(*args, **kwargs):
        entered.set()
        await release.wait()
        if fail:
            raise host.HostError("upstream unavailable")
        return CapabilitySnapshot(
            connection_id=str(row["id"]),
            capability_revision=2,
            config_revision=1,
            protocol_version="2025-11-25",
            server_name="original",
        )

    monkeypatch.setattr(protocol, "discover", discover)
    monkeypatch.setattr(mcp_connections, "McpHost", lambda **kwargs: built)
    task = asyncio.create_task(
        mcp_connections._diagnose(
            connection_id=row["id"],
            user={"user_id": "owner"},
            store=store,
            record=True,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    return store, row, built, transport, release, task


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "save_failure"])
async def test_diagnostic_releases_its_session_on_every_exit(monkeypatch, outcome):
    store, row, built, transport, release, task = await setup_diagnostic(
        monkeypatch, fail=outcome == "failure"
    )
    try:
        if outcome == "save_failure":
            monkeypatch.setattr(
                store,
                "save_mcp_capabilities",
                AsyncMock(side_effect=RuntimeError("db unavailable")),
            )
        if outcome == "cancel":
            task.cancel()
        release.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "save_failure":
            with pytest.raises(RuntimeError, match="db unavailable"):
                await task
        else:
            result = await task
            assert result.ok is (outcome == "success")
        assert transport.closed
        assert (
            built.session(host.SessionKey(connection_id=str(row["id"]), member="diagnostic"))
            is None
        )
    finally:
        release.set()
        await built.aclose()


@pytest.mark.parametrize(
    "changed", ["config_revision", "authorization_epoch", "capability_revision", "deleted_at"]
)
@pytest.mark.parametrize("fail", [False, True])
async def test_diagnostic_refuses_a_result_after_connection_changes(monkeypatch, changed, fail):
    store, row, built, transport, release, task = await setup_diagnostic(monkeypatch, fail=fail)
    current = store.connections[row["id"]]
    current[changed] = 2
    current["status"] = "draft"
    store.capabilities[row["id"]] = {"server_name": "newer result"}
    release.set()
    try:
        with pytest.raises(HTTPException) as exc:
            await task
        assert exc.value.status_code == 409
        assert store.capabilities[row["id"]] == {"server_name": "newer result"}
        assert current["status"] == "draft"
        assert transport.closed
    finally:
        await built.aclose()
