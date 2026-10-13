"""Regressions for credential routing and retained upstream response bounds."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from cloud_agent.api import mcp_connections, mcp_relay
from cloud_agent.mcp import catalog, host, oauth, protocol, refresh, sse, transports
from cloud_agent.mcp.jsonrpc import decode, id_key
from cloud_agent.mcp.models import AuthKind, AuthProfile, HeaderAuth, McpServer, Transport
from cloud_agent.mcp.net_policy import EgressPolicy, EndpointRejected, normalize_url
from cloud_agent.mcp.registry import resolve_connection
from cloud_agent.mcp.transports import LegacySseTransport, RemoteTransportConfig, TransportError


def _config(**overrides: Any) -> RemoteTransportConfig:
    settings = {
        "endpoint": normalize_url("https://127.0.0.1:9/sse"),
        "auth": AuthProfile(kind=AuthKind.STATIC),
        "policy": EgressPolicy(allow_private=True),
        "secret": "synthetic-credential",
    }
    settings.update(overrides)
    return RemoteTransportConfig(**settings)


def _response(request_id: str, value: Any) -> Any:
    return decode(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": value}))[0]


class _UnconsumedBody(httpx.AsyncByteStream):
    """Record whether rejection happens before the upstream body is pulled."""

    def __init__(self) -> None:
        self.pulled = False
        self.closed = False

    async def __aiter__(self):
        self.pulled = True
        raise AssertionError("a compressed response must be rejected before reading its body")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


def _compressed_reply(body: _UnconsumedBody, content_type: str = "application/json"):
    return httpx.Response(
        200, headers={"content-encoding": "gzip", "content-type": content_type}, stream=body
    )


def test_sse_bounds_one_event_across_many_short_complete_lines(monkeypatch):
    monkeypatch.setattr(sse, "MAX_EVENT_BYTES", 128)
    decoder = sse.SseDecoder()
    for _ in range(3):
        assert decoder.feed(b"data: " + b"x" * 32 + b"\n") == []
    with pytest.raises(sse.SseError, match="event is larger"):
        decoder.feed(b"data: " + b"x" * 32 + b"\n")
    assert sum(len(value) for value in decoder._data) == 96


def test_sse_counts_fields_and_resets_the_event_bound(monkeypatch):
    monkeypatch.setattr(sse, "MAX_EVENT_BYTES", 32)
    decoder = sse.SseDecoder()
    decoder.feed(b"id: 1234\nevent: response\n")
    with pytest.raises(sse.SseError, match="event is larger"):
        decoder.feed(b"data: " + b"x" * 24 + b"\n")
    decoder = sse.SseDecoder()
    for _ in range(20):
        assert decoder.feed(b"data: 12345678901234567890\n\n")[0].data == "12345678901234567890"
        assert decoder.feed(b": keepalive\n") == []


def test_sse_bounds_complete_lines_as_well_as_unfinished_tails(monkeypatch):
    monkeypatch.setattr(sse, "_MAX_LINE_BYTES", 16)
    with pytest.raises(sse.SseError, match="line longer"):
        sse.SseDecoder().feed(b"data: " + b"x" * 17 + b"\n\n")


@pytest.mark.parametrize("text", ["café", "你好", "👩🏽‍💻"])
def test_sse_preserves_utf8_at_every_possible_chunk_split(text):
    frame = ("data: " + json.dumps({"text": text}, ensure_ascii=False) + "\r\n\r\n").encode()
    for split in range(1, len(frame)):
        decoder = sse.SseDecoder()
        events = decoder.feed(frame[:split]) + decoder.feed(frame[split:]) + decoder.close()
        assert [event.json() for event in events] == [{"text": text}]


def test_sse_flushes_an_incomplete_utf8_character_once_on_close():
    decoder = sse.SseDecoder()
    assert decoder.feed(b"data: caf\xc3") == []
    assert decoder.close()[0].data == "caf�"


@pytest.mark.parametrize("value, expected", [("0", 0), ("00050", 50), ("2147483647", 2147483647)])
def test_sse_preserves_bounded_ascii_retry_delays(value, expected):
    events = sse.SseDecoder().feed(f"retry: {value}\ndata: ready\n\n".encode())
    assert events[0].retry == expected


@pytest.mark.parametrize("value", ["²", "١٢٣", "\uff11\uff12\uff13", "-1", "+1", "1.5", "", " 5"])
def test_sse_ignores_malformed_retry_fields_without_replacing_valid_delay(value):
    decoder = sse.SseDecoder()
    assert decoder.feed(b"retry: 1500\n") == []
    events = decoder.feed(f"retry: {value}\ndata: ready\n\n".encode())
    assert events[0].retry == 1500


@pytest.mark.parametrize("value", [b"9" * 5000, b"2147483648", b"0" * 11])
@pytest.mark.parametrize("terminated", [True, False])
def test_sse_rejects_oversized_retry_values_with_a_bounded_typed_error(value, terminated):
    decoder = sse.SseDecoder()
    frame = b"retry: " + value
    with pytest.raises(sse.SseError, match="retry delay beyond") as raised:
        if terminated:
            decoder.feed(frame + b"\n")
        else:
            assert decoder.feed(frame) == []
            decoder.close()
    assert len(str(raised.value)) < 100


@pytest.mark.parametrize(
    "name", ["Host", "Cookie", "Content-Length", "X-Forwarded-For", "x-real-ip", "Accept-Encoding"]
)
def test_owner_api_rejects_forbidden_static_credential_headers(name):
    with pytest.raises(HTTPException) as raised:
        mcp_connections._build_auth("static", name, "")
    assert raised.value.status_code == 400


@pytest.mark.parametrize("name", ["Host", "X-Forwarded-For"])
def test_dispatch_rejects_forbidden_headers_in_existing_static_profiles(name):
    profile = AuthProfile(kind=AuthKind.STATIC, header=HeaderAuth(name, ""))
    with pytest.raises(EndpointRejected):
        profile.header.render("synthetic")
    with pytest.raises(TransportError, match="reserved or forwarding"):
        transports.build_headers(_config(auth=profile), body=True)
    request = Request({"type": "http", "headers": []})
    server = McpServer(
        name="example",
        connection_id="1",
        transport=Transport.STREAMABLE_HTTP,
        url="https://127.0.0.1:9/mcp",
        headers={name: "synthetic"},
    )
    with pytest.raises(HTTPException) as raised:
        mcp_relay._upstream_headers(request, server)
    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_resolving_an_existing_forbidden_profile_holds_its_credential():
    row = {
        "id": 1,
        "user_id": "owner",
        "label": "example",
        "transport": "streamable-http",
        "location": "remote",
        "url": "https://127.0.0.1:9/mcp",
        "auth_kind": "static",
        "auth_config": {"header_name": "Host", "scheme": ""},
        "access_token_ciphertext": "synthetic-ciphertext",
    }
    resolved = await resolve_connection(
        AsyncMock(),
        row=row,
        cipher=SimpleNamespace(decrypt=lambda _: "synthetic"),
        load_snapshot=False,
    )
    assert not resolved.usable
    assert resolved.credential == {}


@pytest.mark.asyncio
async def test_static_secret_with_line_break_is_rejected_before_storage():
    store = SimpleNamespace(mcp=AsyncMock())
    with pytest.raises(HTTPException) as raised:
        await mcp_connections._store_static_secret(
            store,
            row={"id": 1, "authorization_epoch": 1},
            secret="synthetic\r\nHost: internal.example",
            header_name="Authorization",
            header_scheme="Bearer",
            replaces_identity=True,
        )
    assert raised.value.status_code == 400
    store.mcp.set_mcp_credential.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:9/messages",
        "https://127.0.0.1:10/messages",
        "https://elsewhere.example/messages",
    ],
)
async def test_every_legacy_endpoint_announcement_is_validated_before_mutation(target):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(202))
    ) as http:
        transport = LegacySseTransport(_config(), http, message_url="https://127.0.0.1:9/messages")
        with pytest.raises((TransportError, EndpointRejected)):
            await transport._handle_event(sse.SseEvent(event="endpoint", data=target))
        assert transport._message_url == "https://127.0.0.1:9/messages"


@pytest.mark.asyncio
async def test_raw_server_answers_recheck_the_target_before_sending():
    called = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: called.append(request) or httpx.Response(202))
    ) as http:
        transport = LegacySseTransport(
            _config(policy=EgressPolicy()), http, message_url="https://127.0.0.1:9/messages"
        )
        await transport._post_raw(b'{"jsonrpc":"2.0","id":1,"result":{"roots":[]}}')
    assert called == []


@pytest.mark.asyncio
async def test_allowed_later_relative_endpoint_routes_an_answer_with_identity_encoding():
    called = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: called.append(request) or httpx.Response(202))
    ) as http:
        transport = LegacySseTransport(_config(), http, message_url="https://127.0.0.1:9/initial")
        await transport._handle_event(
            sse.SseEvent(event="endpoint", data="/messages?session=second")
        )
        await transport._handle_event(
            sse.SseEvent(data='{"jsonrpc":"2.0","id":"server-1","method":"roots/list"}')
        )
    assert len(called) == 1
    assert str(called[0].url) == "https://127.0.0.1:9/messages?session=second"
    assert called[0].headers["authorization"] == "Bearer synthetic-credential"
    assert called[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_unsolicited_and_duplicate_legacy_responses_do_not_grow_the_inbox():
    async with httpx.AsyncClient() as http:
        transport = LegacySseTransport(_config(), http)
        for index in range(100):
            transport.deliver(_response(str(index), {"text": "x" * 1024}))
        assert transport._inbox == {}
        key = id_key("pending")
        waiter = transport._waiter(key)
        transport.deliver(_response("pending", {"text": "first"}))
        transport.deliver(_response("pending", {"text": "duplicate"}))
        assert waiter.is_set()
        assert list(transport._inbox) == [key]
        assert transport._inbox[key].result() == {"text": "first"}


@pytest.mark.asyncio
async def test_a_cancelled_legacy_request_removes_its_pending_mailbox():
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def handler(_):
        entered.set()
        await hold.wait()
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = LegacySseTransport(_config(), http, message_url="https://127.0.0.1:9/messages")
        task = asyncio.create_task(transport.request("tools/list"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        transport.deliver(_response("l1", {}))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport._waiters == {}
        assert transport._inbox == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["event", "complete_line", "eof_event", "retry"])
@pytest.mark.parametrize("reader", ["streamable", "legacy"])
async def test_sse_limits_are_typed_failures_with_session_cleanup(monkeypatch, reader, limit):
    if limit == "retry":
        body = b"retry: " + b"9" * 5000 + b"\n"
    elif limit == "complete_line":
        monkeypatch.setattr(sse, "_MAX_LINE_BYTES", 32)
        body = b":" + b"x" * 33 + b"\n"
    else:
        monkeypatch.setattr(sse, "MAX_EVENT_BYTES", 32)
        body = b"data: " + b"x" * 32
        if limit == "event":
            body = (b"data: " + b"x" * 16 + b"\n") * 3
    replies = []

    def handler(_):
        reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        replies.append(reply)
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        if reader == "legacy":
            transport = LegacySseTransport(_config(), http)
            announcement = transport.announcement()
            waiter = transport._waiter(id_key("pending"))
            response = handler(None)
            with pytest.raises(TransportError) as raised:
                await transport.consume(response)
            assert raised.value.kind == "protocol_incompatible"
            with pytest.raises(TransportError) as announced:
                await announcement
            assert announced.value is raised.value
            assert transport._ended and waiter.is_set()
        else:
            transport = transports.StreamableHttpTransport(_config(), http, owns_client=True)
            session = host.Session(
                key=host.SessionKey(connection_id="1", member="job:1"),
                transport=transport,
                handshake=protocol.Handshake(protocol_version="2025-11-25"),
            )
            with pytest.raises(host.HostError) as raised:
                await host.McpHost().call_tool(session, name="search")
            assert raised.value.kind == "protocol_incompatible"
            assert not session.reusable and http.is_closed
        assert "x" * 16 not in str(raised.value)
        assert "9" * 16 not in str(raised.value)
        assert len(replies) == 1, "an oversized reply must not replay a possible external write"
        assert replies[0].is_closed


@pytest.mark.asyncio
async def test_approved_dispatch_records_unknown_outcome_after_sse_limit(monkeypatch):
    monkeypatch.setattr(sse, "MAX_EVENT_BYTES", 32)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: " + b"x" * 32 + b"\n\n",
            )
        )
    ) as http:
        transport = transports.StreamableHttpTransport(_config(), http, owns_client=True)
        session = host.Session(
            key=host.SessionKey(connection_id="1", member="job:1"),
            transport=transport,
            handshake=protocol.Handshake(protocol_version="2025-11-25"),
        )
        authorized = SimpleNamespace(job_id="job", alias="example", dispatches=[(7, 2)])
        capability = SimpleNamespace(name="search")
        entry = (authorized, capability)
        monkeypatch.setattr(mcp_relay, "_host_catalogue", AsyncMock(return_value={"tool": entry}))
        monkeypatch.setattr(mcp_relay, "_refresh_catalogue_entry", AsyncMock(return_value=entry))
        monkeypatch.setattr(mcp_relay, "_decide_call", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_relay, "_host_session", AsyncMock(return_value=session))
        built = host.McpHost()
        monkeypatch.setattr(mcp_relay, "_host", lambda: built)
        store = SimpleNamespace(mcp=AsyncMock())
        reply = await mcp_relay._host_call(
            {"params": {"name": "tool"}},
            request_id="request",
            identity={},
            store=store,
            authorization=None,
            stateless=True,
        )
    assert "error" in reply
    assert "oversized SSE event" in reply["error"]["message"]
    store.mcp.finish_mcp_invocation.assert_awaited_once_with(
        invocation_id=7, dispatch_generation=2, status="outcome_unknown"
    )
    assert authorized.dispatches == []
    assert not session.reusable and http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["body", "post_stream", "legacy_stream", "event_iterator"])
async def test_compressed_responses_are_refused_before_any_body_is_pulled(reader):
    body = _UnconsumedBody()
    response = _compressed_reply(body, "text/event-stream")
    async with httpx.AsyncClient() as http:
        transport = LegacySseTransport(_config(), http)
        with pytest.raises((TransportError, httpx.DecodingError), match="compressed response"):
            if reader == "body":
                await transports.read_bounded(response)
            elif reader == "post_stream":
                await transport._drain_sse(response, expect_key=None)
            elif reader == "legacy_stream":
                await transport.consume(response)
            else:
                async for _ in transports.iter_sse(response):
                    pass
    await response.aclose()
    assert not body.pulled
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
async def test_raw_relay_rejects_compression_before_returning_a_stream(monkeypatch, content_type):
    body = _UnconsumedBody()
    seen = []

    def handler(request):
        seen.append(request)
        return _compressed_reply(body, content_type)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(mcp_relay, "guard_target", AsyncMock())
    monkeypatch.setattr(mcp_relay, "build_upstream_client", lambda _: http)
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as raised:
        await mcp_relay._proxy(
            request,
            server=McpServer(
                name="example",
                connection_id="1",
                transport=Transport.STREAMABLE_HTTP,
                url="https://example.invalid/mcp",
            ),
            job_id="job",
            method="POST",
        )
    assert raised.value.status_code == 502
    assert seen[0].headers["accept-encoding"] == "identity"
    assert not body.pulled
    assert body.closed and http.is_closed


@pytest.mark.asyncio
async def test_metadata_negotiates_identity_and_rejects_a_compressed_reply():
    body = _UnconsumedBody()
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: seen.append(request) or _compressed_reply(body)
        )
    ) as http:
        with pytest.raises(TransportError, match="compressed response"):
            await transports.HttpMetadataFetcher(client=http).fetch_json(
                "https://127.0.0.1:9/metadata", policy=EgressPolicy(allow_private=True)
            )
    assert seen[0].headers["accept-encoding"] == "identity"
    assert not body.pulled


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["registration", "token", "refresh"])
async def test_oauth_posts_negotiate_identity_and_refuse_compression(monkeypatch, operation):
    body = _UnconsumedBody()
    seen = []
    original_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return original_client(
            transport=httpx.MockTransport(
                lambda request: seen.append(request) or _compressed_reply(body)
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    policy = EgressPolicy(allow_private=True)
    token_request = oauth.TokenRequest(
        url="https://127.0.0.1:9/token",
        data={"grant_type": "authorization_code", "code": "synthetic"},
        client=oauth.ClientIdentity(client_id="client", registration="preregistered"),
    )
    with pytest.raises(
        (HTTPException, TransportError, refresh.RefreshRefused), match="compressed response"
    ):
        if operation == "registration":
            await mcp_connections._post_json(None, token_request.url, {}, policy=policy)
        elif operation == "token":
            await mcp_connections._post_form(token_request, policy=policy)
        else:
            await refresh._post(token_request, policy=policy, fetcher=None)
    assert seen[0].headers["accept-encoding"] == "identity"
    assert not body.pulled and body.closed


@pytest.mark.asyncio
async def test_registry_pages_negotiate_identity_and_refuse_compression():
    body = _UnconsumedBody()
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: seen.append(request) or _compressed_reply(body)
        )
    ) as http:
        with pytest.raises(httpx.DecodingError, match="compressed response"):
            await catalog.fetch_catalog(http)
    assert seen[0].headers["accept-encoding"] == "identity"
    assert not body.pulled and body.closed
