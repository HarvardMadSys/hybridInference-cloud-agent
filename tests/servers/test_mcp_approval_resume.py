"""The original HTTP tool call resumes after a decision, with no second prompt."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from tests.servers.test_mcp_relay_host import (
    _ATTEMPT,
    _JOB,
    _LEASE,
    FakeStore,
    StubHost,
    _auth,
    _binding,
    _rpc,
    _secrets as _secrets,
)

from cloud_agent.api import mcp_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp.host import McpHost
from cloud_agent.mcp.jsonrpc import JsonRpcError
from cloud_agent.mcp.protocol import Handshake, ProtocolError
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.mcp.transports import TransportError

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
    message = response.json()["error"]["message"]
    if change == "timeout":
        assert "owner's decision" in message
        assert "not sent" in message
        assert "Approvals" in message
    elif change in {"lease", "connection", "policy"}:
        assert "authorization changed" in message
        assert "not sent" in message
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


@pytest.mark.parametrize("status", ["dispatching", "completed", "failed", "outcome_unknown"])
async def test_another_dispatchers_outcome_is_not_reported_as_an_unsent_call(
    client, waiting_store, upstream, status
):
    task = await _pending_call(client, waiting_store)
    waiting_store.wait_action = status
    response = await asyncio.wait_for(task, timeout=3)
    message = response.json()["error"]["message"]
    assert "already claimed for dispatch" in message
    assert "Check the external state" in message
    assert "not dispatched" not in message
    assert upstream.called == []


async def test_losing_the_dispatch_claim_does_not_invite_an_unsafe_write_retry(
    client, waiting_store, upstream, monkeypatch
):
    task = await _pending_call(client, waiting_store)

    async def lost_claim(**_kwargs):
        return None

    monkeypatch.setattr(waiting_store, "claim_mcp_invocation", lost_claim)
    waiting_store.approved = {**waiting_store.created_invocations[0], "status": "approved"}
    response = await asyncio.wait_for(task, timeout=3)
    message = response.json()["error"]["message"]
    assert "another caller may already have sent it" in message
    assert "Check the external state" in message
    assert upstream.called == []


async def _streamed_call(store):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "headers": [], "app": FastAPI()}, receive)
    identity = await store.resolve_model_credential(
        job_id=_JOB, attempt_id=_ATTEMPT, lease_generation=_LEASE
    )
    return mcp_relay._host_call_stream(
        document=_rpc("tools/call", name="github__get_issue", arguments={"id": 7}),
        identity=identity,
        store=store,
        request=request,
        authorization=_auth()["Authorization"],
    )


async def test_streamed_approval_sends_keepalives_then_one_result(
    waiting_store, upstream, monkeypatch
):
    monkeypatch.setattr(mcp_relay, "_HOST_KEEPALIVE_S", 0.02)
    stream = await _streamed_call(waiting_store)
    try:
        assert (await anext(stream)).startswith(b":")
        await asyncio.wait_for(waiting_store.invocation_created.wait(), timeout=2)
        assert (await anext(stream)).startswith(b":")
        assert upstream.called == []
        waiting_store.approved = {**waiting_store.created_invocations[0], "status": "approved"}
        async for frame in stream:
            if frame.startswith(b"event: message"):
                payload = json.loads(frame.split(b"data: ", 1)[1].strip())
                assert payload["id"] == 1
                assert payload["result"]["content"][0]["text"] == "upstream says hi"
                break
        else:
            pytest.fail("the stream ended without the held call's result")
    finally:
        await stream.aclose()
    assert len(upstream.called) == 1
    assert len(waiting_store.claimed) == 1
    assert waiting_store.finished[0]["status"] == "completed"


async def test_disconnected_stream_expires_approval_before_dispatch(waiting_store, upstream):
    stream = await _streamed_call(waiting_store)
    await anext(stream)
    await asyncio.wait_for(waiting_store.invocation_created.wait(), timeout=2)
    await stream.aclose()
    assert waiting_store.created_invocations[0]["status"] == "expired"
    assert upstream.called == []
    assert waiting_store.claimed == []


async def test_disconnected_stream_records_an_unknown_dispatched_outcome(
    waiting_store, upstream, monkeypatch
):
    started = asyncio.Event()
    release = asyncio.Event()
    original = upstream.call_tool

    async def hanging(*args, **kwargs):
        result = await original(*args, **kwargs)
        started.set()
        await release.wait()
        return result

    monkeypatch.setattr(upstream, "call_tool", hanging)
    stream = await _streamed_call(waiting_store)
    await anext(stream)
    await asyncio.wait_for(waiting_store.invocation_created.wait(), timeout=2)
    waiting_store.approved = {**waiting_store.created_invocations[0], "status": "approved"}
    await asyncio.wait_for(started.wait(), timeout=2)
    await stream.aclose()
    assert len(upstream.called) == 1
    assert waiting_store.finished[0]["status"] == "outcome_unknown"


@pytest.mark.parametrize(
    "accept,streamed",
    [
        ("application/json, text/event-stream", True),
        ("text/event-stream; q=0.5", True),
        ("application/json", False),
        ("*/*", False),
        ("text/event-stream; q=0", False),
        ("text/event-stream-malformed", False),
    ],
)
async def test_held_call_preserves_the_callers_accepted_response_format(
    client, waiting_store, accept, streamed
):
    waiting_store.wait_action = "expired"
    response = await client.post(
        "/v1/agent/mcp",
        json=_rpc("tools/call", name="github__get_issue", arguments={"id": 7}),
        headers={**_auth(), "Accept": accept},
    )
    expected = "text/event-stream" if streamed else "application/json"
    assert response.headers["content-type"].startswith(expected)
    if streamed:
        assert response.content.startswith(b":")
        assert b'"id":1' in response.content
        assert b"approval expired" in response.content
    else:
        assert response.json()["id"] == 1


async def test_asgi_disconnect_does_not_leave_a_streamed_approval_running(waiting_store, upstream):
    app = FastAPI()
    app.include_router(mcp_relay.router)
    app.dependency_overrides[get_storage] = lambda: waiting_store
    incoming = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": json.dumps(
                _rpc("tools/call", name="github__get_issue", arguments={"id": 7})
            ).encode(),
            "more_body": False,
        }
    )
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "POST",
        "scheme": "http",
        "path": "/v1/agent/mcp",
        "query_string": b"",
        "headers": [
            (b"authorization", _auth()["Authorization"].encode()),
            (b"accept", b"text/event-stream"),
        ],
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await asyncio.wait_for(waiting_store.invocation_created.wait(), timeout=2)
        assert any(item.get("body", b"").startswith(b":") for item in sent)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=2)
        assert waiting_store.created_invocations[0]["status"] == "expired"
        assert upstream.called == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
        async def request(self, method, params=None, **_kwargs):
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
