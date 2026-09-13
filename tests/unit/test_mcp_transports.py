"""The HTTP transport, tested where it decides rather than where it dials.

`transports.py` sits under every remote connection, and until now it was only
covered *through* the layers above it — the relay tests, the host integration
test and the external end-to-end run. Those prove the path works; they do not
prove what happens at the edges, and the edges are where this module's decisions
live:

- **Which headers travel.** A connection's own headers are merged
  case-insensitively, and the credential the platform resolved wins under the
  same name. A naive dict update keeps both spellings, httpx sends both, and the
  sandbox's own token rides along beside the credential meant to replace it.
- **What a status means.** 401 is "reconnect this", 404 is "that address has
  nothing", anything else is the server answering badly. Collapsing them gives
  the owner an action that does not apply.
- **How a reply is found.** A Streamable HTTP answer is one JSON object or a
  request-scoped stream, and the answer may arrive after progress notifications.
- **Which era the server speaks.** The probe's outcome decides, and a server
  that has never heard of `server/discover` has to fall back to the handshake
  rather than fail.

The transport is mocked; everything above it is the code that ships.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cloud_agent.mcp.jsonrpc import JsonRpcError
from cloud_agent.mcp.models import AuthKind, AuthProfile, HeaderAuth
from cloud_agent.mcp.net_policy import EgressPolicy, normalize_url
from cloud_agent.mcp.protocol import ProtocolError
from cloud_agent.mcp.transports import (
    LegacySseTransport,
    RemoteTransportConfig,
    StreamableHttpTransport,
    TransportError,
    build_headers,
    connect_streamable,
)

pytestmark = pytest.mark.asyncio

#: A literal loopback address, so the egress guard needs no resolver. The
#: private rule is widened for it the way a deployment with a private connector
#: bridge widens it; the scheme rule stays at its https default because the
#: mocked transport never opens a socket.
_URL = "https://127.0.0.1:9/mcp"


def _config(**overrides: Any) -> RemoteTransportConfig:
    """One remote connection, with the loopback host the guard will accept."""
    settings: dict[str, Any] = {
        "endpoint": normalize_url(_URL),
        "auth": AuthProfile(kind=AuthKind.NONE),
        "policy": EgressPolicy(allow_private=True),
        "secret": None,
        "headers": {},
        "protocol_version": "",
    }
    settings.update(overrides)
    return RemoteTransportConfig(**settings)


def _client(handler: Any) -> httpx.AsyncClient:
    """An HTTP client whose every request is answered by ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_body(request: httpx.Request) -> dict[str, Any]:
    """The JSON-RPC request the transport sent."""
    return json.loads(request.content or b"{}")


def _reply(request: httpx.Request, result: Any, **headers: str) -> httpx.Response:
    """Answer one JSON-RPC request with a result."""
    body = json.loads(request.content or b"{}")
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": body.get("id"), "result": result},
        headers={"content-type": "application/json", **headers},
    )


def _sse(payloads: list[Any], **headers: str) -> httpx.Response:
    """Answer with a request-scoped SSE stream carrying ``payloads``."""
    frames = b"".join(b"data: " + json.dumps(payload).encode() + b"\n\n" for payload in payloads)
    return httpx.Response(
        200,
        content=frames,
        headers={"content-type": "text/event-stream", **headers},
    )


class TestHeaders:
    """What actually goes out, and what must not."""

    async def test_the_platform_credential_wins_under_the_same_name(self) -> None:
        """Header names are case-insensitive; ``dict`` keys are not.

        A connection configured with ``authorization`` while the platform writes
        ``Authorization`` would otherwise send both, and the sandbox's own token
        would ride along beside the credential meant to replace it.
        """
        config = _config(
            auth=AuthProfile(kind=AuthKind.STATIC, header=HeaderAuth(header_name="Authorization")),
            secret="connection-credential",
            headers={"authorization": "Bearer sandbox-token", "x-tenant": "harvard"},
        )
        headers = build_headers(config, body=True)
        # One name, one entry: read back case-insensitively, because the point
        # is that exactly one survives rather than which spelling it uses.
        credential = next(value for key, value in headers.items() if key.lower() == "authorization")
        assert credential == "Bearer connection-credential"
        # And the ordinary user header survives.
        assert headers["x-tenant"] == "harvard"
        # Exactly one spelling of the credential name made it out.
        assert len([key for key in headers if key.lower() == "authorization"]) == 1

    async def test_an_oauth_credential_is_a_bearer_token(self) -> None:
        """Whatever the profile, the header this platform sends is one form."""
        config = _config(
            auth=AuthProfile(
                kind=AuthKind.OAUTH,
                oauth=__import__("cloud_agent.mcp.models", fromlist=["OAuthProfile"]).OAuthProfile(
                    issuer="https://issuer.example.com"
                ),
            ),
            secret="oauth-access-token",
        )
        headers = build_headers(config, body=True)
        assert any(
            key.lower() == "authorization" and value == "Bearer oauth-access-token"
            for key, value in headers.items()
        )

    async def test_the_protocol_revision_is_declared_when_known(self) -> None:
        """A server that requires it rejects the request without it."""
        config = _config(protocol_version="2025-11-25", session_id="sess-1")
        headers = build_headers(config, body=False)
        assert headers["mcp-protocol-version"] == "2025-11-25"
        assert headers["mcp-session-id"] == "sess-1"


class TestStatuses:
    """Turning an HTTP answer into an action the owner can take."""

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_credential_asks_for_a_reconnect(self, status: int) -> None:
        """The only failure here the owner can fix themselves."""
        client = _client(lambda request: httpx.Response(status, content=b"{}"))
        transport = StreamableHttpTransport(_config(), client)
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "needs_authorization"
        assert raised.value.status == status
        await client.aclose()

    async def test_a_missing_endpoint_is_reported_as_unreachable(self) -> None:
        """Not as a bad credential: the address has nothing at that path."""
        client = _client(lambda request: httpx.Response(404, content=b"{}"))
        transport = StreamableHttpTransport(_config(), client)
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "unreachable"
        await client.aclose()

    async def test_any_other_failure_keeps_the_body_for_diagnosis(self) -> None:
        """The body is what era detection reads, so it is not discarded."""
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32022, "message": "no"}}
        ).encode()
        client = _client(lambda request: httpx.Response(400, content=payload))
        transport = StreamableHttpTransport(_config(), client)
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "protocol_incompatible"
        assert raised.value.modern_error() is not None
        await client.aclose()


class TestStreamableReplies:
    """Finding the answer, whichever shape it arrives in."""

    async def test_a_json_reply_is_read_directly(self) -> None:
        """The common case."""
        client = _client(lambda request: _reply(request, {"tools": []}))
        transport = StreamableHttpTransport(_config(), client)
        assert await transport.request("tools/list") == {"tools": []}
        await client.aclose()

    async def test_the_answer_is_found_after_a_progress_notification(self) -> None:
        """A stream carries everything the server has to say, not only the reply.

        Scanning for the request's id rather than taking the first message is
        what makes a call that reports progress before it answers work at all.
        """
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(_json_body(request))
            return _sse(
                [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progress": 1},
                    },
                    {"jsonrpc": "2.0", "id": seen["id"], "result": {"ok": True}},
                ]
            )

        client = _client(handler)
        transport = StreamableHttpTransport(_config(), client)
        assert await transport.request("tools/call") == {"ok": True}
        await client.aclose()

    async def test_a_notification_is_answered_with_nothing(self) -> None:
        """202 with no body is the only valid shape for one."""
        client = _client(lambda request: httpx.Response(202, content=b""))
        transport = StreamableHttpTransport(_config(), client)
        assert await transport.notify("notifications/initialized") is None
        await client.aclose()

    async def test_a_reply_carrying_another_id_is_reported_not_returned(self) -> None:
        """A response to something else is a protocol fault, not this answer."""
        client = _client(
            lambda request: httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": "not-ours", "result": {}},
                headers={"content-type": "application/json"},
            )
        )
        transport = StreamableHttpTransport(_config(), client)
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "protocol_incompatible"
        await client.aclose()

    async def test_a_session_the_server_assigns_is_reused(self) -> None:
        """Streamable HTTP needs it on every later request."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("mcp-session-id"))
            return _reply(request, {}, **{"mcp-session-id": "sess-9"})

        client = _client(handler)
        transport = StreamableHttpTransport(_config(), client)
        await transport.request("tools/list")
        await transport.request("tools/call")
        assert seen == [None, "sess-9"]
        await client.aclose()


class TestEraDetection:
    """Which shape the server turned out to speak."""

    def _with_discovery(self, handler: Any) -> httpx.AsyncClient:
        return _client(handler)

    async def test_a_legacy_server_falls_back_to_the_handshake(self) -> None:
        """A plain "no such method" is a server describing itself.

        Over a success status this is the common shape -- a server that has
        never heard of ``server/discover`` answers with the ordinary JSON-RPC
        code rather than an HTTP error. Reading it as a modern refusal failed
        the connection against every server the handshake era covers.
        """
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = str(_json_body(request).get("method"))
            methods.append(method)
            if method == "server/discover":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": _json_body(request)["id"],
                        "error": {"code": -32601, "message": "method not found"},
                    },
                    headers={"content-type": "application/json"},
                )
            if method == "initialize":
                return _reply(request, {"protocolVersion": "2025-11-25", "capabilities": {}})
            return httpx.Response(202, content=b"")

        client = self._with_discovery(handler)
        transport, handshake = await connect_streamable(_config(), client=client)
        assert methods == ["server/discover", "initialize"]
        assert handshake.protocol_version == "2025-11-25"
        assert transport.era == "handshake"
        await client.aclose()

    async def test_an_unsupported_version_is_retried_once_with_one_it_offers(self) -> None:
        """The list the server sends is the only thing that makes the retry
        supported rather than a guess."""
        attempted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = _json_body(request)
            if body.get("method") == "server/discover":
                version = str(
                    (body.get("params") or {})
                    .get("_meta", {})
                    .get("io.modelcontextprotocol/protocolVersion", "")
                )
                attempted.append(version)
                if version == "2026-07-28":
                    return httpx.Response(
                        400,
                        json={
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {
                                "code": -32022,
                                "message": "unsupported",
                                "data": {"supported": ["2025-11-25"]},
                            },
                        },
                        headers={"content-type": "application/json"},
                    )
                return _reply(
                    request,
                    {
                        "supportedVersions": ["2025-11-25"],
                        "capabilities": {"tools": {}},
                        "_meta": {
                            "io.modelcontextprotocol/serverInfo": {
                                "name": "modern",
                                "version": "1",
                            }
                        },
                    },
                )
            return httpx.Response(202, content=b"")

        client = _client(handler)
        transport, handshake = await connect_streamable(_config(), client=client)
        assert attempted == ["2026-07-28", "2025-11-25"]
        assert handshake.protocol_version == "2025-11-25"
        assert transport.era == "modern"
        await client.aclose()

    async def test_a_modern_refusal_is_not_answered_with_a_handshake(self) -> None:
        """A server that has removed ``initialize`` must not be asked for one."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = _json_body(request)
            return httpx.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32021, "message": "missing client capability"},
                },
                headers={"content-type": "application/json"},
            )

        client = _client(handler)
        with pytest.raises(TransportError):
            await connect_streamable(_config(), client=client)
        await client.aclose()


class TestLegacyMessageEndpoint:
    """The announced POST endpoint is a second address, so it is checked."""

    async def test_an_announcement_on_another_origin_is_refused(self) -> None:
        """Following it would spend the user's credential on a host they never
        approved."""
        client = _client(lambda request: httpx.Response(200, content=b""))
        transport = LegacySseTransport(
            _config(), client, message_url="https://elsewhere.example.com/messages"
        )
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "blocked_target"
        assert "elsewhere.example.com" in str(raised.value)
        await client.aclose()


class TestHandshake:
    """The legacy era's own negotiation."""

    async def test_a_server_sharing_no_revision_is_refused(self) -> None:
        """Continuing would fail later in a way that reads like our bug."""
        client = _client(lambda request: _reply(request, {"supportedVersions": ["1999-01-01"]}))
        with pytest.raises((TransportError, JsonRpcError, ProtocolError)):
            await connect_streamable(
                _config(policy=EgressPolicy(allow_private=True)), client=client
            )
        await client.aclose()
