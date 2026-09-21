"""Session owners release sockets on disconnect, cancellation and job completion."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.test_mcp_host_sessions_and_results import Transport, session
from tests.unit.test_mcp_protocol_and_authorization import _connection
from tests.unit.test_mcp_transports import _client, _config

from cloud_agent.api import mcp_relay
from cloud_agent.app import lifespan
from cloud_agent.mcp import host, protocol, transports

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("owned", [True, False])
async def test_streamable_session_releases_only_its_own_client(monkeypatch, owned):
    http = _client(lambda request: httpx.Response(204))
    monkeypatch.setattr(transports, "build_client", lambda config: http)
    monkeypatch.setattr(
        transports, "discover_server", AsyncMock(return_value=protocol.Handshake("2025-11-25"))
    )
    transport, _ = await transports.connect_streamable(_config(), client=None if owned else http)
    await session(transport).aclose()
    try:
        assert http.is_closed is owned
    finally:
        await http.aclose()


class EventStream(httpx.AsyncByteStream):
    """A legacy server that holds its stream open until its owner closes it."""

    def __init__(self, announcement):
        self.announcement = announcement
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        if self.announcement:
            yield f"event: endpoint\ndata: {self.announcement}\n\n".encode()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("owned", [True, False])
@pytest.mark.parametrize("outcome", ["success", "off_origin", "cancel", "handshake_error"])
async def test_legacy_connect_cleans_up_every_exit(monkeypatch, owned, outcome):
    endpoint = "https://127.0.0.2:9/messages" if outcome == "off_origin" else "/messages"
    stream = EventStream(None if outcome == "cancel" else endpoint)
    http = _client(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(transports, "build_client", lambda config: http)
    handshake = AsyncMock(return_value=protocol.Handshake("2025-11-25"))
    if outcome == "handshake_error":
        handshake.side_effect = protocol.ProtocolError("invalid handshake")
    monkeypatch.setattr(transports, "initialize", handshake)
    connecting = asyncio.create_task(
        transports.connect_legacy_sse(_config(), client=None if owned else http)
    )
    await stream.started.wait()
    try:
        if outcome == "success":
            transport, _, reader = await connecting
            await session(transport).aclose()
            assert reader.done()
        elif outcome == "cancel":
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting
        else:
            with pytest.raises((transports.TransportError, protocol.ProtocolError)):
                await connecting
        assert stream.closed
        assert http.is_closed is owned
    finally:
        await http.aclose()


@pytest.mark.parametrize("owned", [True, False])
async def test_legacy_failed_get_preserves_borrowed_client(monkeypatch, owned):
    def refused(request):
        raise httpx.ConnectError("offline")

    http = _client(refused)
    monkeypatch.setattr(transports, "build_client", lambda config: http)
    try:
        with pytest.raises(transports.TransportError):
            await transports.connect_legacy_sse(_config(), client=None if owned else http)
        assert http.is_closed is owned
    finally:
        await http.aclose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_termination_cannot_skip_local_cleanup(monkeypatch, cancel):
    monkeypatch.setattr(host, "SESSION_CLOSE_TIMEOUT_S", 0.02, raising=False)
    transport = Transport()
    terminating = asyncio.Event()

    async def terminate():
        terminating.set()
        await asyncio.Event().wait()

    transport.terminate = terminate
    current = session(transport)
    closing = asyncio.create_task(current.aclose())
    await terminating.wait()
    if cancel:
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await asyncio.wait_for(closing, timeout=0.5)
    assert transport.closed
    await current.aclose()


async def _opened(monkeypatch, *members):
    built = host.McpHost()

    async def connect(connection, *, secret):
        return Transport(), protocol.Handshake("2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    sessions = [
        await built.open(
            _connection(), secret=None, key=host.SessionKey(connection_id="7", member=member)
        )
        for member in members
    ]
    return built, sessions


async def test_reaper_preserves_live_fork_and_closes_completed_job(monkeypatch):
    built, (finished, running) = await _opened(monkeypatch, "parent:1", "fork:2")
    assert await built.reap_inactive(lambda key: _live(key, "fork:2")) == 1
    assert finished.transport.closed
    assert built.session(finished.key) is None
    assert built.session(running.key) is running
    assert not running.transport.closed
    await built.aclose()
    assert running.transport.closed


async def test_shutdown_closes_a_connection_that_finishes_opening_late(monkeypatch):
    built = host.McpHost()
    started, release = asyncio.Event(), asyncio.Event()
    transport = Transport()

    async def connect(connection, *, secret):
        started.set()
        await release.wait()
        return transport, protocol.Handshake("2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    key = host.SessionKey(connection_id="7", member="job:1")
    opening = asyncio.create_task(built.open(_connection(), secret=None, key=key))
    await started.wait()
    await built.aclose()
    release.set()
    with pytest.raises(host.HostError, match="shutting down"):
        await opening
    assert transport.closed
    assert built.session(key) is None
    with pytest.raises(host.HostError, match="shutting down"):
        await built.open(_connection(), secret=None, key=key)


async def _live(key, member):
    return key.member == member


async def test_reaper_does_not_close_a_replacement_after_stale_lookup(monkeypatch):
    built, (old,) = await _opened(monkeypatch, "job:1")
    entered, release = asyncio.Event(), asyncio.Event()

    async def live(key):
        entered.set()
        await release.wait()
        return False

    reaping = asyncio.create_task(built.reap_inactive(live))
    await entered.wait()
    replacement = await built.open(_connection(), secret="rotated", key=old.key)
    release.set()
    assert await reaping == 0
    assert built.session(old.key) is replacement
    assert not replacement.transport.closed
    await built.aclose()


async def test_reaper_keeps_sessions_when_authority_lookup_fails(monkeypatch):
    built, (current,) = await _opened(monkeypatch, "job:1")
    assert await built.reap_inactive(AsyncMock(side_effect=RuntimeError("database offline"))) == 0
    assert built.session(current.key) is current
    assert not current.transport.closed
    await built.aclose()


async def test_one_stalled_authority_lookup_does_not_block_other_cleanup(monkeypatch):
    built, (stalled, finished) = await _opened(monkeypatch, "stalled:1", "ended:2")
    monkeypatch.setattr(host, "SESSION_CHECK_TIMEOUT_S", 0.02)

    async def live(key):
        if key == stalled.key:
            await asyncio.Event().wait()
        return False

    assert await asyncio.wait_for(built.reap_inactive(live), timeout=0.5) == 1
    assert not stalled.transport.closed
    assert finished.transport.closed
    await built.aclose()


async def test_reaper_bounds_concurrent_database_lookups(monkeypatch):
    built, sessions = await _opened(monkeypatch, *(f"job-{index}:1" for index in range(20)))
    active = peak = 0

    async def live(key):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        return False

    assert await built.reap_inactive(live) == 20
    assert 1 < peak <= 8
    assert all(item.transport.closed for item in sessions)


@pytest.mark.parametrize(
    "state", ["live", "ended", "removed", "disabled", "deleted", "disconnected", "missing"]
)
async def test_maintenance_checks_current_job_and_connection_authority(monkeypatch, state):
    built, (current,) = await _opened(monkeypatch, "job:1")
    binding = {"connection_id": 7, "owner_id": "owner", "enabled": state != "disabled"}
    identity = {
        "user_id": "owner",
        "mcp_bindings": [] if state == "removed" else [binding],
    }
    store = SimpleNamespace(
        credential_queries=SimpleNamespace(
            resolve_model_credential=AsyncMock(return_value=None if state == "ended" else identity)
        ),
        mcp=SimpleNamespace(
            get_mcp_connection=AsyncMock(
                return_value=(
                    None
                    if state == "missing"
                    else {
                        "status": "disabled" if state == "disconnected" else "connected",
                        "deleted_at": 1 if state == "deleted" else None,
                    }
                )
            ),
            expire_mcp_invocations=AsyncMock(),
        ),
    )
    monkeypatch.setattr(mcp_relay, "_HOST_INSTANCE", built)
    await mcp_relay.reap_host_sessions(store)
    assert current.transport.closed is (state != "live")
    await mcp_relay.close_host()
    assert current.transport.closed
    assert mcp_relay._HOST_INSTANCE is None


async def test_maintenance_retries_after_expiry_failure_and_stops_on_cancel(monkeypatch):
    swept_twice = asyncio.Event()
    sweeps = 0

    async def reap(store):
        nonlocal sweeps
        sweeps += 1
        if sweeps == 2:
            swept_twice.set()

    expiry = AsyncMock(side_effect=[RuntimeError("database restarting"), 1])
    store = SimpleNamespace(mcp=SimpleNamespace(expire_mcp_invocations=expiry))
    monkeypatch.setattr(mcp_relay, "_HOST_MAINTENANCE_INTERVAL_S", 0.001)
    monkeypatch.setattr(mcp_relay, "reap_host_sessions", reap)
    task = asyncio.create_task(mcp_relay.maintain_host_sessions(store))
    try:
        await asyncio.wait_for(swept_twice.wait(), timeout=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert expiry.await_count == 2


async def test_relay_lifespan_runs_maintenance_and_releases_upstream_sessions(monkeypatch):
    built, (current,) = await _opened(monkeypatch, "job:1")
    monkeypatch.setattr(mcp_relay, "_HOST_INSTANCE", built)
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setattr("asyncpg.create_pool", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace", AsyncMock(return_value=None)
    )
    started = asyncio.Event()

    async def maintain(store):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_relay, "maintain_host_sessions", maintain)
    app = FastAPI()
    async with lifespan(app, background=False):
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert app.state.reaper_task is None
        assert app.state.publish_task is None
        assert not current.transport.closed
    assert app.state.mcp_maintenance_task.cancelled()
    assert current.transport.closed
