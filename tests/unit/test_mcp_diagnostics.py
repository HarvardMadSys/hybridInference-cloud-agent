"""A slow connection test cannot publish stale results or leak its transport."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from cloud_agent.api import mcp_connections
from cloud_agent.mcp import host, protocol
from cloud_agent.mcp.models import CapabilitySnapshot
from cloud_agent.mcp.refresh import RefreshRefused
from cloud_agent.mcp.registry import mcp_cipher
from tests.unit.test_mcp_connections_api import FakeStore
from tests.unit.test_mcp_host_sessions_and_results import Transport

pytestmark = pytest.mark.asyncio


async def setup_diagnostic(monkeypatch, *, fail=False, authenticated=False):
    """Open a real host session and pause only its upstream discovery reply."""
    store = FakeStore()
    row = await store.create_mcp_connection(
        user_id="owner",
        label="Docs",
        transport="streamable-http",
        location="remote",
        url="https://docs.example.com/mcp",
        auth_kind="static" if authenticated else "none",
    )
    if authenticated:
        monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "diagnostic-test-key")
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=row["authorization_epoch"],
            access_token_ciphertext=mcp_cipher().encrypt("old-token"),
        )
        row = await store.get_mcp_connection(connection_id=row["id"])
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


@pytest.mark.parametrize("fail", [False, True])
async def test_rotated_credential_invalidates_in_flight_diagnostic(monkeypatch, fail):
    store, row, built, transport, release, task = await setup_diagnostic(
        monkeypatch, fail=fail, authenticated=True
    )
    await store.set_mcp_credential(
        connection_id=row["id"],
        expected_authorization_epoch=row["authorization_epoch"],
        access_token_ciphertext=mcp_cipher().encrypt("new-token"),
        bump_epoch=False,
    )
    current = store.connections[row["id"]]
    assert current["authorization_epoch"] == row["authorization_epoch"]
    assert current["credential_version"] == row["credential_version"] + 1
    current["status"] = "draft"
    store.capabilities[row["id"]] = {"server_name": "newer result"}
    release.set()
    try:
        with pytest.raises(HTTPException) as exc:
            await task
        assert exc.value.status_code == 409
        assert current["status"] == "draft"
        assert store.capabilities[row["id"]] == {"server_name": "newer result"}
        assert transport.closed
    finally:
        await built.aclose()


async def test_losing_refresh_cannot_mark_replacement_credential_as_failed(monkeypatch):
    store = FakeStore()
    row = await store.create_mcp_connection(
        user_id="owner",
        label="Docs",
        transport="streamable-http",
        location="remote",
        auth_kind="oauth",
    )

    async def replaced_during_refresh(*args, **kwargs):
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=row["authorization_epoch"],
            access_token_ciphertext="replacement-ciphertext",
        )
        raise RefreshRefused("a newer authorization replaced the refresh")

    monkeypatch.setattr(mcp_connections, "ensure_fresh", replaced_during_refresh)
    with pytest.raises(HTTPException) as exc:
        await mcp_connections._diagnose(
            connection_id=row["id"], user={"user_id": "owner"}, store=store, record=True
        )
    assert exc.value.status_code == 409
    assert store.connections[row["id"]]["status"] == "draft"


async def test_diagnostic_can_publish_with_its_own_refreshed_credential(monkeypatch):
    async def refresh_before_discovery(store, *, row, policy):
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=row["authorization_epoch"],
            access_token_ciphertext=mcp_cipher().encrypt("refreshed-token"),
        )
        return await store.get_mcp_connection(connection_id=row["id"])

    monkeypatch.setattr(mcp_connections, "ensure_fresh", refresh_before_discovery)
    store, row, built, transport, release, task = await setup_diagnostic(
        monkeypatch, authenticated=True
    )
    release.set()
    try:
        assert (await task).ok
        assert store.connections[row["id"]]["credential_version"] == row["credential_version"] + 1
        assert store.connections[row["id"]]["status"] == "active"
        assert transport.closed
    finally:
        await built.aclose()


async def test_rotation_after_discovery_commit_cannot_publish_stale_success(monkeypatch):
    store, row, built, transport, release, task = await setup_diagnostic(
        monkeypatch, authenticated=True
    )
    save = store.save_mcp_capabilities

    async def save_then_rotate(**kwargs):
        revision = await save(**kwargs)
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=row["authorization_epoch"],
            access_token_ciphertext=mcp_cipher().encrypt("replacement-token"),
            reset_status=True,
        )
        return revision

    monkeypatch.setattr(store, "save_mcp_capabilities", save_then_rotate)
    release.set()
    try:
        with pytest.raises(HTTPException) as exc:
            await task
        assert exc.value.status_code == 409
        assert store.connections[row["id"]]["status"] == "draft"
        assert transport.closed
    finally:
        await built.aclose()
