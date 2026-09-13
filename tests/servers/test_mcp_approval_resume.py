"""The original HTTP tool call resumes after a decision, with no second prompt."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import mcp_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp.host import McpHost
from cloud_agent.mcp.jsonrpc import JsonRpcError
from cloud_agent.mcp.protocol import Handshake, ProtocolError
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.mcp.transports import TransportError
from tests.servers.test_mcp_relay_host import (
    FakeStore,
    StubHost,
    _auth,
    _binding,
    _rpc,
    _secrets as _secrets,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def waiting_store():
    """A live attempt with one tool that requires an owner decision."""
    store = FakeStore(
        bindings=[
            _binding(
                7,
                "github",
                rules={"get_issue": {"policy": "confirm", "fingerprint": ""}},
            )
        ]
    )
    store.wait_action = "pending"
    return store


@pytest.fixture
def upstream(monkeypatch):
    """Observe external dispatch separately from the pending approval."""
    host = StubHost()
    monkeypatch.setattr(mcp_relay, "_HOST_INSTANCE", host)
    return host


@pytest.fixture
async def client(waiting_store, upstream):
    """Serve the production router with a controllable persistence port."""
    app = FastAPI()
    app.include_router(mcp_relay.router)
    app.dependency_overrides[get_storage] = lambda: waiting_store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


async def _pending_call(client, store):
    task = asyncio.create_task(
        client.post(
            "/v1/agent/mcp",
            json=_rpc("tools/call", name="github__get_issue", arguments={"id": 7}),
            headers=_auth(),
        )
    )
    await asyncio.wait_for(store.invocation_created.wait(), timeout=2)
    assert not task.done()
    return task


async def test_approve_resumes_the_original_call_once(client, waiting_store, upstream):
    task = await _pending_call(client, waiting_store)
    assert upstream.called == []
    invocation = waiting_store.created_invocations[0]
    # A token refresh while approval is pending must not dispatch the old key.
    waiting_store.connections[7]["access_token_ciphertext"] = mcp_cipher().encrypt("refreshed-key")
    waiting_store.approved = {**invocation, "status": "approved"}
    response = await asyncio.wait_for(task, timeout=3)
    assert "result" in response.json()
    assert len(upstream.called) == 1
    assert upstream.opened[0]["secret"] == "refreshed-key"
    assert waiting_store.claimed == [invocation["id"]]
    assert waiting_store.finished == [
        {
            "invocation_id": invocation["id"],
            "dispatch_generation": 1,
            "status": "completed",
        }
    ]


@pytest.mark.parametrize("change", ["deny", "lease", "connection", "policy", "timeout"])
async def test_a_stale_or_refused_approval_never_dispatches(
    client,
    waiting_store,
    upstream,
    change,
):
    task = await _pending_call(client, waiting_store)
    if change == "deny":
        waiting_store.wait_action = "denied"
    elif change == "lease":
        waiting_store.live = False
    elif change == "connection":
        waiting_store.connections[7]["authorization_epoch"] += 1
    elif change == "policy":
        waiting_store.bindings[0]["policy_revision"] += 1
    else:
        waiting_store.created_invocations[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
    response = await asyncio.wait_for(task, timeout=3)
    assert "error" in response.json()
    assert upstream.called == []
    assert waiting_store.claimed == []


async def test_cancelled_caller_leaves_no_approval_to_reuse(client, waiting_store, upstream):
    task = await _pending_call(client, waiting_store)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiting_store.created_invocations[0]["status"] == "expired"
    assert waiting_store.claimed == []
    assert upstream.called == []


@pytest.mark.parametrize(
    "failure",
    [
        TransportError("upstream timed out"),
        JsonRpcError(-32603, "upstream failed"),
        ProtocolError("malformed upstream reply"),
    ],
)
async def test_upstream_failure_finishes_approval_and_returns_a_protocol_error(
    client,
    waiting_store,
    monkeypatch,
    failure,
):
    calls = []

    class BrokenTransport:
        async def request(self, method, params):
            calls.append(method)
            raise failure

    async def connect(connection, *, secret):
        return BrokenTransport(), Handshake(protocol_version="2025-11-25"), None

    actual_host = McpHost()
    monkeypatch.setattr(actual_host, "_connect", connect)
    monkeypatch.setattr(mcp_relay, "_HOST_INSTANCE", actual_host)
    task = await _pending_call(client, waiting_store)
    invocation = waiting_store.created_invocations[0]
    waiting_store.approved = {**invocation, "status": "approved"}
    response = await asyncio.wait_for(task, timeout=3)
    assert response.status_code == 200
    assert response.json()["error"]["message"] == str(failure)
    assert calls == ["tools/call"]
    assert waiting_store.finished == [
        {
            "invocation_id": invocation["id"],
            "dispatch_generation": 1,
            "status": "outcome_unknown",
        }
    ]
