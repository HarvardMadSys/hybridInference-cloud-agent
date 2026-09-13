"""The legacy HTTP+SSE transport, end to end over a mocked stream.

The 2024-11-05 shape is the only transport here that carries replies on a
different channel from the requests: the client opens one GET stream, reads an
``endpoint`` event naming where requests go, POSTs them there, and receives every
reply on the stream it already holds. Nothing in this repository exercised that
flow until now -- the origin check was the only part of it under test -- and it
is a flow with three places to lose a message:

- the announcement may never arrive, or may name somewhere else;
- a frame may be split across a chunk boundary, including the boundary of the
  chunk the announcement itself was read from;
- the stream may end while a request is still waiting.

The stream is a real ``AsyncByteStream`` rather than a fixed body, because a
mock that hands the whole body over at once cannot show where one chunk ends and
the next begins -- which is exactly where this transport's decisions live.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from cloud_agent.mcp.models import AuthKind, AuthProfile
from cloud_agent.mcp.net_policy import EgressPolicy, normalize_url
from cloud_agent.mcp.transports import (
    LegacySseTransport,
    RemoteTransportConfig,
    TransportError,
    connect_legacy_sse,
)

pytestmark = pytest.mark.asyncio

_URL = "https://127.0.0.1:9/mcp"
_MESSAGES = "https://127.0.0.1:9/messages"


def _config(**overrides: Any) -> RemoteTransportConfig:
    """One legacy connection against the loopback host the guard accepts."""
    settings: dict[str, Any] = {
        "endpoint": normalize_url(_URL),
        "auth": AuthProfile(kind=AuthKind.NONE),
        "policy": EgressPolicy(allow_private=True, allow_plain_http=True),
        "secret": None,
        "headers": {},
        "protocol_version": "",
        "connect_timeout": 1.0,
    }
    settings.update(overrides)
    return RemoteTransportConfig(**settings)


class _Stream(httpx.AsyncByteStream):
    """A server stream the test drives chunk by chunk."""

    def __init__(self) -> None:
        """Start with nothing queued and no readers waiting."""
        self._chunks: list[bytes] = []
        self._ready = asyncio.Event()
        self._closed = False

    def push(self, chunk: bytes) -> None:
        """Queue one chunk and wake the reader."""
        self._chunks.append(chunk)
        self._ready.set()

    def end(self) -> None:
        """End the stream, releasing any reader."""
        self._closed = True
        self._ready.set()

    async def __aiter__(self) -> Any:
        """Yield queued chunks as they arrive, then finish."""
        while True:
            await self._ready.wait()
            while self._chunks:
                yield self._chunks.pop(0)
            if self._closed:
                return
            self._ready.clear()

    async def aclose(self) -> None:
        """Nothing to release: the queue is in memory."""
        self._closed = True
        self._ready.set()


def _frame(event: str, data: Any) -> bytes:
    """One SSE frame, with the payload JSON-encoded."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class _LegacyServer:
    """A stand-in 2024-11-05 server: one stream, one message endpoint."""

    def __init__(self, *, announce: bool = True, announce_relative: bool = False) -> None:
        """Configure what the announcement looks like, if there is one."""
        self.stream = _Stream()
        self.posts: list[dict[str, Any]] = []
        self.announce = announce
        self.announce_relative = announce_relative
        self._announced = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Serve the GET stream and the POST message endpoint."""
        if request.method == "GET":
            asyncio.get_running_loop().call_soon(self._announce)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=self.stream,
            )
        body = json.loads(request.content or b"{}")
        self.posts.append(body)
        asyncio.get_running_loop().call_soon(self._answer, body)
        return httpx.Response(202, content=b"")

    def _announce(self) -> None:
        """Push the endpoint event, once."""
        if self._announced or not self.announce:
            return
        self._announced = True
        target = "/messages" if self.announce_relative else _MESSAGES
        # The endpoint event carries the URL as its data, not JSON-encoded:
        # it is the one event in this transport that is not a JSON-RPC message.
        self.stream.push(f"event: endpoint\ndata: {target}\n\n".encode())

    def _answer(self, body: dict[str, Any]) -> None:
        """Push the reply for one POSTed request."""
        method = body.get("method")
        if method == "initialize":
            result: Any = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": "get_issue"}]}
        else:
            result = {}
        self.stream.push(
            _frame("message", {"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        )


async def _connect(server: _LegacyServer, **overrides: Any) -> Any:
    """Open a legacy connection against ``server``."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return await connect_legacy_sse(_config(**overrides), client=client), client


class TestTheAnnouncement:
    """Finding where requests go, and refusing to guess."""

    async def test_a_relative_announcement_resolves_against_the_stream(self) -> None:
        """The endpoint names a path; the origin is the one we already trust."""
        server = _LegacyServer(announce_relative=True)
        (transport, handshake, _task), client = await _connect(server)
        assert transport._message_url == _MESSAGES
        assert handshake.protocol_version == "2025-11-25"
        await transport.aclose()
        await client.aclose()

    async def test_a_stream_that_never_announces_is_refused(self) -> None:
        """Waiting forever would look like a hung job, not a broken server."""
        server = _LegacyServer(announce=False)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
        with pytest.raises(TransportError) as raised:
            await connect_legacy_sse(_config(connect_timeout=0.05), client=client)
        assert raised.value.kind == "protocol_incompatible"
        await client.aclose()

    async def test_an_announcement_that_leaves_the_origin_is_refused(self) -> None:
        """Following it would spend the user's credential on a host they never
        approved, which is the whole reason the announcement is checked."""
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b""))
        )
        transport = LegacySseTransport(
            _config(), client, message_url="https://elsewhere.example.com/messages"
        )
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "blocked_target"
        await client.aclose()


class TestTheFlow:
    """Request out on the POST, reply back on the stream."""

    async def test_a_request_is_answered_from_the_stream(self) -> None:
        """The ordinary case, and the only one the transport exists for."""
        server = _LegacyServer()
        (transport, _handshake, _task), client = await _connect(server)
        listed = await transport.request("tools/list")
        assert listed == {"tools": [{"name": "get_issue"}]}
        # Both requests went to the announced endpoint, not to the stream's URL.
        assert [post["method"] for post in server.posts] == ["initialize", "tools/list"]
        await transport.aclose()
        await client.aclose()

    async def test_a_frame_split_across_chunks_is_reassembled(self) -> None:
        """A chunk boundary falls anywhere, including inside the JSON."""
        server = _LegacyServer()
        (transport, _handshake, _task), client = await _connect(server)

        async def split_answer(body: dict[str, Any]) -> None:
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}}
            ).encode()
            server.stream.push(b"data: " + payload[:12])
            await asyncio.sleep(0)
            server.stream.push(payload[12:] + b"\n\n")

        server._answer = lambda body: asyncio.ensure_future(split_answer(body))  # type: ignore[method-assign]
        assert await transport.request("tools/list") == {"ok": True}
        await transport.aclose()
        await client.aclose()

    async def test_ending_the_stream_releases_a_waiting_request(self) -> None:
        """Otherwise a job holds its request open until its own timeout, for a
        connection that is already gone."""
        server = _LegacyServer()
        client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
        (transport, _handshake, _task) = await connect_legacy_sse(_config(), client=client)

        original = server._answer

        def silence(body: dict[str, Any]) -> None:
            if body.get("method") == "tools/list":
                server.stream.end()
                return
            original(body)

        server._answer = silence  # type: ignore[method-assign]
        with pytest.raises(TransportError):
            await transport.request("tools/list")
        await transport.aclose()
        await client.aclose()
