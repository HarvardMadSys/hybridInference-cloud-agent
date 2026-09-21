"""Concurrent upstream sessions and the MCP results consumed by native clients."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.unit.test_mcp_protocol_and_authorization import _connection

from cloud_agent.mcp import host, protocol
from cloud_agent.mcp.transports import TransportError


class Transport:
    """Observe whether retiring a cached session interrupts a running request."""

    def __init__(self, result=None):
        self.result = result or {"content": [{"type": "text", "text": "done"}]}
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def request(self, method, params=None):
        self.started.set()
        await self.release.wait()
        assert not self.closed, "the request's transport was closed while it was running"
        return self.result

    async def aclose(self):
        self.closed = True


def session(transport):
    return host.Session(
        key=host.SessionKey(connection_id="7", member="job:1"),
        transport=transport,
        handshake=protocol.Handshake(protocol_version="2025-11-25"),
    )


@pytest.mark.asyncio
async def test_next_explicit_call_reconnects_after_transport_failure_without_replaying(monkeypatch):
    built = host.McpHost()
    opened = []

    class BrokenTransport(Transport):
        async def request(self, method, params=None):
            raise TransportError("connection closed")

    async def connect(connection, *, secret):
        transport = BrokenTransport() if not opened else Transport()
        opened.append(transport)
        return transport, protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    key = host.SessionKey(connection_id="7", member="job:1")
    first = await built.open(_connection(), secret=None, key=key)
    with pytest.raises(host.HostError, match="connection closed"):
        await built.call_tool(first, name="search")
    assert len(opened) == 1, "a possibly dispatched call must not be replayed automatically"
    assert opened[0].closed
    second = await built.open(_connection(), secret=None, key=key)
    result = await built.call_tool(second, name="search")
    assert result.content[0].text == "done"
    assert len(opened) == 2
    await built.close(key)


@pytest.mark.asyncio
async def test_concurrent_first_calls_share_one_upstream_session(monkeypatch):
    built = host.McpHost()
    entered, release = asyncio.Event(), asyncio.Event()
    opened = []

    async def connect(connection, *, secret):
        transport = Transport()
        opened.append(transport)
        entered.set()
        await release.wait()
        return transport, protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    key = host.SessionKey(connection_id="7", member="job:1")
    first = asyncio.create_task(built.open(_connection(), secret=None, key=key))
    await entered.wait()
    second = asyncio.create_task(built.open(_connection(), secret=None, key=key))
    await asyncio.sleep(0)
    release.set()
    left, right = await asyncio.gather(first, second)
    assert left is right
    assert len(opened) == 1
    await built.close(key)
    assert all(transport.closed for transport in opened)


@pytest.mark.asyncio
async def test_close_during_connect_does_not_leave_a_resurrected_session(monkeypatch):
    built = host.McpHost()
    entered, release = asyncio.Event(), asyncio.Event()
    transport = Transport()

    async def connect(connection, *, secret):
        entered.set()
        await release.wait()
        return transport, protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    key = host.SessionKey(connection_id="7", member="job:1")
    opening = asyncio.create_task(built.open(_connection(), secret=None, key=key))
    await entered.wait()
    closing = asyncio.create_task(built.close(key))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(opening, closing)
    assert built.session(key) is None
    assert transport.closed


@pytest.mark.asyncio
async def test_a_slow_connection_does_not_block_another_member(monkeypatch):
    built = host.McpHost()
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(connection, *, secret):
        if secret == "slow":
            entered.set()
            await release.wait()
        return Transport(), protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    slow_key = host.SessionKey(connection_id="7", member="slow:1")
    fast_key = host.SessionKey(connection_id="7", member="fast:1")
    slow = asyncio.create_task(built.open(_connection(), secret="slow", key=slow_key))
    await entered.wait()
    try:
        await asyncio.wait_for(built.open(_connection(), secret="fast", key=fast_key), timeout=1)
        assert not slow.done()
    finally:
        release.set()
        await slow
        await built.close(slow_key)
        await built.close(fast_key)


@pytest.mark.asyncio
async def test_a_new_attempt_reaps_old_transports_without_closing_a_fork(monkeypatch):
    built = host.McpHost()

    async def connect(connection, *, secret):
        return Transport(), protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    old = host.SessionKey(connection_id="7", member="job:1")
    fork = host.SessionKey(connection_id="7", member="child:2")
    new = host.SessionKey(connection_id="7", member="job:3")
    original = await built.open(_connection(), secret=None, key=old)
    child = await built.open(_connection(), secret=None, key=fork)
    await built.open(_connection(), secret=None, key=new)
    assert built.session(old) is None
    assert original.transport.closed
    assert not child.transport.closed
    with pytest.raises(host.HostError, match="superseded"):
        await built.open(_connection(), secret=None, key=old)
    await built.close(new)
    await built.close(fork)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["config_revision", "authorization_epoch"])
async def test_a_changed_target_or_authorization_closes_the_old_session(monkeypatch, change):
    built = host.McpHost()

    async def connect(connection, *, secret):
        return Transport(), protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    key = host.SessionKey(connection_id="7", member="job:1")
    original = await built.open(_connection(), secret="old", key=key)
    async with original.using():
        await built.open(_connection(**{change: 2}), secret="new", key=key)
        assert original.transport.closed
    await built.close(key)


@pytest.mark.asyncio
async def test_credential_rotation_does_not_interrupt_an_inflight_tool(monkeypatch):
    built = host.McpHost()
    opened = []

    async def connect(connection, *, secret):
        transport = Transport()
        opened.append(transport)
        return transport, protocol.Handshake(protocol_version="2025-11-25"), None

    monkeypatch.setattr(built, "_connect", connect)
    key = host.SessionKey(connection_id="7", member="job:1")
    first = await built.open(_connection(), secret="old", key=key)
    opened[0].release.clear()
    running = asyncio.create_task(built.call_tool(first, name="search"))
    await opened[0].started.wait()
    try:
        second = await built.open(_connection(), secret="new", key=key)
        assert second is not first
        assert not opened[0].closed
    finally:
        opened[0].release.set()
        await running
    assert opened[0].closed
    assert not opened[1].closed
    await built.close(key)


@pytest.mark.asyncio
async def test_resources_read_preserves_mcp_contents():
    contents = [
        {"uri": "docs://readme", "mimeType": "text/plain", "text": "hello"},
        {"uri": "docs://image", "mimeType": "image/png", "blob": "aGVsbG8="},
    ]
    result = await host.McpHost().read_resource(
        session(Transport({"contents": contents})), uri="docs://readme"
    )
    assert result["contents"] == contents
    json.dumps(result)


@pytest.mark.asyncio
async def test_prompt_content_is_json_serializable_with_mcp_field_names():
    messages = [
        {
            "role": "assistant",
            "content": {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        }
    ]
    result = await host.McpHost().get_prompt(
        session(Transport({"messages": messages})), name="help"
    )
    assert json.loads(json.dumps(result))["messages"] == messages


@pytest.mark.parametrize(
    "block",
    [
        {"type": "text", "text": ""},
        {
            "type": "resource",
            "resource": {"uri": "docs://readme", "text": "hello", "mimeType": "text/plain"},
        },
        {
            "type": "resource",
            "resource": {"uri": "docs://image", "blob": "aGVsbG8=", "mimeType": "image/png"},
        },
        {
            "type": "resource_link",
            "uri": "docs://readme",
            "name": "Readme",
            "description": "Project overview",
        },
    ],
)
def test_tool_content_keeps_required_mcp_fields(block):
    result = host.McpHost().render_result({"content": [block]}).as_dict()
    assert result["content"] == [block]


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "data": "a" * 32, "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "docs://image", "blob": "a" * 32}},
    ],
)
def test_oversized_binary_content_is_refused_instead_of_forwarded_unbounded(block):
    built = host.McpHost(limits=host.HostLimits(max_content_bytes=16))
    with pytest.raises(host.HostError):
        built.render_result({"content": [block]})


def test_embedded_resource_text_is_bounded_by_bytes_not_characters():
    built = host.McpHost(limits=host.HostLimits(max_content_bytes=16))
    block = {"type": "resource", "resource": {"uri": "docs://readme", "text": "文" * 16}}
    result = built.render_result({"content": [block]}).as_dict()
    assert len(result["content"][0]["resource"]["text"].encode()) <= 16


def test_oversized_structured_result_does_not_bypass_the_content_limit():
    built = host.McpHost(limits=host.HostLimits(max_content_bytes=16))
    with pytest.raises(host.HostError):
        built.render_result({"structuredContent": {"text": "文" * 16}})
