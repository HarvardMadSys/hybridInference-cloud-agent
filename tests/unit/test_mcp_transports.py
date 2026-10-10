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

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

import httpcore
import httpx
import pytest

from cloud_agent.mcp import transports

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from cloud_agent.mcp.jsonrpc import JsonRpcError
from cloud_agent.mcp.models import AuthKind, AuthProfile, HeaderAuth
from cloud_agent.mcp.net_policy import EgressPolicy, normalize_url
from cloud_agent.mcp.protocol import ProtocolError
from cloud_agent.mcp.transports import (
    LegacySseTransport,
    RemoteTransportConfig,
    StreamableHttpTransport,
    TransportError,
    build_client,
    build_headers,
    connect_legacy_sse,
    connect_streamable,
    guard_target,
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


async def test_existing_transport_uses_updated_default_timeouts_and_keeps_overrides(monkeypatch):
    values = {"mcp_request_timeout_s": 120.0, "mcp_connect_timeout_s": 10.0}
    monkeypatch.setattr(
        transports, "number_setting", lambda name, default=None: values.get(name, default)
    )
    timeouts = []

    def handle(request):
        timeouts.append(request.extensions["timeout"])
        return _reply(request, {"tools": []})

    async with _client(handle) as http:
        implicit = StreamableHttpTransport(_config(), http)
        explicit = StreamableHttpTransport(_config(request_timeout=17, connect_timeout=3), http)
        await implicit.request("tools/list")
        values.update(mcp_request_timeout_s=31.0, mcp_connect_timeout_s=2.0)
        await implicit.request("tools/list")
        await explicit.request("tools/list")
    assert [timeout["read"] for timeout in timeouts] == [120, 31, 17]
    assert [timeout["connect"] for timeout in timeouts] == [10, 2, 3]


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


class _CountingStream(httpx.AsyncByteStream):
    """A body that records how much of itself the platform was willing to read."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._count):
            self.pulled += 1
            yield self._chunk

    async def aclose(self) -> None:
        """Nothing to release; the bytes are in memory already."""


class _HeldOpenStream(httpx.AsyncByteStream):
    """An SSE body whose second half waits for the client to answer the first."""

    def __init__(self, opening: bytes, closing: bytes, released: asyncio.Event) -> None:
        self._opening = opening
        self._closing = closing
        self._released = released

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._opening
        await self._released.wait()
        yield self._closing

    async def aclose(self) -> None:
        """Nothing to release; the frames are in memory already."""


def _frame(payload: Any) -> bytes:
    """One SSE frame, framed the way the decoder expects."""
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class TestWhatTheTransportIsWillingToRead:
    """The limit has to bound allocation, and the stream has to stay interactive.

    Both come from the same mistake: a reply read on the caller's behalf. A
    server that answers with a body nothing bounds chooses how much this
    process allocates, and a server that asks a question mid-stream waits for
    an answer the caller never gets to send, because it is still waiting for
    the body to finish.
    """

    async def test_an_oversized_reply_is_refused_while_it_is_still_arriving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping at the cap, not at the end and then comparing lengths."""
        monkeypatch.setattr(transports, "MAX_RESPONSE_BYTES", 1024)
        stream = _CountingStream(b"x" * 256, 64)
        client = _client(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        )
        transport = StreamableHttpTransport(_config(), client, owns_client=True)
        with pytest.raises(TransportError):
            await transport.request("tools/list")
        assert stream.pulled < 64, "the whole body was read before the limit applied"
        await client.aclose()

    async def test_a_server_request_is_answered_before_the_stream_finishes(self) -> None:
        """The deadlock: the client waits for the body, the server for the answer."""
        released = asyncio.Event()
        answers: list[bytes] = []

        class _AnswerStream(httpx.AsyncByteStream):
            """The 202's body, which records that it was released."""

            def __init__(self) -> None:
                self.closed = False

            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b""

            async def aclose(self) -> None:
                self.closed = True

        answer_stream = _AnswerStream()

        def handler(request: httpx.Request) -> httpx.Response:
            message = json.loads(request.content or b"{}")
            if "method" not in message:
                # The client's answer to the server's own request.
                answers.append(request.content)
                released.set()
                return httpx.Response(202, stream=answer_stream)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_HeldOpenStream(
                    _frame({"jsonrpc": "2.0", "id": 77, "method": "roots/list"}),
                    _frame({"jsonrpc": "2.0", "id": message.get("id"), "result": {"ok": True}}),
                    released,
                ),
            )

        client = _client(handler)
        transport = StreamableHttpTransport(_config(), client, owns_client=True)
        # The handshake era is the one where a server may ask a question
        # mid-reply; the stateless revision forbids it and answers with an
        # input-required result instead of expecting an answer.
        transport.set_era("handshake")
        result = await asyncio.wait_for(transport.request("tools/list"), timeout=5)
        assert result == {"ok": True}
        assert answers, "the server's request was never answered"
        # The answer's own reply is a body like any other: leaving it unread
        # hands a live connection back to the pool on every question the
        # server asks.
        assert answer_stream.closed, "the answer's response was never released"
        await client.aclose()


class TestAddressPinning:
    """The dial goes where the check went, not to a second resolver answer.

    A resolve-then-check is only a check if the connection is made to what was
    checked. A name that answered with a public address the first time can
    answer the second lookup with a loopback one, and the platform would have
    validated one server while talking to another.
    """

    async def test_a_checked_name_is_dialled_at_the_address_it_was_checked_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name is looked up to check it, and then dialled at what was checked.

        The dial must not perform a lookup of its own: that second answer is
        the one a rebinding name gets to choose, and it is an answer no policy
        here has seen. So the resolver is made to answer for the name in a way
        the real one cannot -- ``rebind.test`` does not exist -- and the checked
        answer is a literal loopback address. A dial that looks the name up
        again fails outright; a dial that pins reaches the server that passed
        the check, which is the only one that may be reached.
        """
        served: list[int] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                served.append(int(self.server.server_address[1]))
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                """Keep the test output to the test's own assertions."""

        import socketserver

        class _Server(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

        checked = _Server(("127.0.0.1", 0), _Handler)
        threading.Thread(target=checked.serve_forever, daemon=True).start()

        lookups = {"n": 0}
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
            if host != "rebind.test":
                return real_getaddrinfo(host, port, *args, **kwargs)
            lookups["n"] += 1
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1", checked.server_port),
                )
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        config = _config(
            endpoint=normalize_url(f"http://rebind.test:{checked.server_port}/mcp"),
            policy=EgressPolicy(allow_private=True, allow_plain_http=True),
        )
        client = build_client(config)
        transport = StreamableHttpTransport(config, client, owns_client=True)
        try:
            assert await transport.request("tools/list") == {"ok": True}
        finally:
            await client.aclose()
            checked.shutdown()
            checked.server_close()
        assert lookups["n"] == 1, "the dial resolved the name again"
        assert served == [checked.server_port], "the request did not reach the checked server"

    async def test_the_backend_dials_the_pinned_address(self) -> None:
        """The mechanism, without a socket: a checked name keeps its answer."""
        from cloud_agent.mcp.net_policy import (
            UncheckedTarget,
            check_addresses,
            dial_spellings,
            pinned_addresses,
        )
        from cloud_agent.mcp.transports import _PinnedNetworkBackend

        check_addresses("mcp.example.com", ["198.51.100.7"], EgressPolicy(allow_private=True))
        assert pinned_addresses("mcp.example.com") == ("198.51.100.7",)
        # The dialler asks in the encoded spelling, so the pin has to be found
        # under both: a miss is a second lookup, which is the bug.
        check_addresses("münchen.example", ["198.51.100.9"], EgressPolicy(allow_private=True))
        assert len(dial_spellings("münchen.example")) == 2
        for spelling in dial_spellings("münchen.example"):
            assert pinned_addresses(spelling) == ("198.51.100.9",)

        dialled: list[tuple[str, int]] = []
        unreachable = {"::1"}

        class _Inner(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Any = None,
            ) -> Any:
                dialled.append((host, port))
                if host in unreachable:
                    raise OSError("no route to host")
                return None

            async def connect_unix_socket(
                self, path: str, timeout: float | None = None, socket_options: Any = None
            ) -> Any:
                return None

            async def sleep(self, seconds: float) -> None:
                """Nothing to wait for."""

        backend = _PinnedNetworkBackend(_Inner())
        await backend.connect_tcp("mcp.example.com", 443)
        # A dual-stack name whose first address has no route still has the
        # second one, and the same check approved it.
        check_addresses("dual.example", ["::1", "198.51.100.8"], EgressPolicy(allow_private=True))
        await backend.connect_tcp("dual.example", 443)
        assert dialled == [("198.51.100.7", 443), ("::1", 443), ("198.51.100.8", 443)]
        # A name nothing checked is refused rather than resolved: a fallback to
        # the resolver is the lookup the pin exists to remove, and doing it
        # quietly at the socket is indistinguishable from never checking.
        with pytest.raises(UncheckedTarget):
            await backend.connect_tcp("never-checked.example.com", 443)
        assert dialled == [("198.51.100.7", 443), ("::1", 443), ("198.51.100.8", 443)]

    async def test_a_unicode_name_is_dialled_at_the_address_checked_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check spells the name one way and the dialler another.

        A host with a non-ASCII label is validated as the text the user typed
        and dialled after httpcore has encoded it, so a pin recorded under only
        one of the two spellings is never found -- and a pin that is never found
        is a second lookup, which is the whole attack.
        """
        from cloud_agent.mcp.transports import _PinnedNetworkBackend

        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
            if host not in ("münchen.test", "xn--mnchen-3ya.test"):
                return real_getaddrinfo(host, port, *args, **kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        await guard_target("https://münchen.test:9/mcp", EgressPolicy(allow_private=True))

        dialled: list[str] = []

        class _Inner(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Any = None,
            ) -> Any:
                dialled.append(host)
                return None

            async def connect_unix_socket(
                self, path: str, timeout: float | None = None, socket_options: Any = None
            ) -> Any:
                return None

            async def sleep(self, seconds: float) -> None:
                """Nothing to wait for."""

        backend = _PinnedNetworkBackend(_Inner())
        # What httpcore asks for, which is the encoded spelling.
        await backend.connect_tcp("xn--mnchen-3ya.test", 9)
        assert dialled == ["127.0.0.1"]

    async def test_the_legacy_stream_is_checked_before_it_is_opened(self) -> None:
        """The GET is the first socket this connection opens.

        A check that runs on the requests *after* it would run after the dial,
        which is why the legacy path has to validate the endpoint itself.
        """
        client = _client(lambda request: pytest.fail("a target no policy allowed was dialled"))
        with pytest.raises(TransportError):
            # The default policy refuses loopback; nothing may be sent.
            await connect_legacy_sse(_config(policy=EgressPolicy()), client=client)
        await client.aclose()

    async def test_a_read_timeout_arrives_as_a_transport_error(self) -> None:
        """A failure while reading is the class of event callers already handle.

        Left as the raw ``httpx`` error it skips every handler on the way up --
        including the one that gives an approved call a terminal state.
        """

        class _Stalling(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b'{"jsonrpc": "2.0", '
                raise httpx.ReadTimeout("the server stopped talking")

            async def aclose(self) -> None:
                """Nothing to release."""

        client = _client(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=_Stalling()
            )
        )
        transport = StreamableHttpTransport(_config(), client, owns_client=True)
        with pytest.raises(TransportError) as raised:
            await transport.request("tools/list")
        assert raised.value.kind == "unreachable"
        await client.aclose()

    async def test_the_addresses_tried_share_one_connect_budget(self) -> None:
        """Three addresses are not three timeouts.

        Each attempt taking the whole budget turns a 40ms connect into 120ms,
        which the caller did not ask for and cannot see.
        """
        from cloud_agent.mcp.net_policy import check_addresses
        from cloud_agent.mcp.transports import _PinnedNetworkBackend

        seen: list[float | None] = []

        class _Burning(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Any = None,
            ) -> Any:
                seen.append(timeout)
                await asyncio.sleep(timeout or 0)
                raise OSError("no route to host")

            async def connect_unix_socket(
                self, path: str, timeout: float | None = None, socket_options: Any = None
            ) -> Any:
                return None

            async def sleep(self, seconds: float) -> None:
                """Nothing to wait for."""

        check_addresses(
            "budget.example",
            ["198.51.100.1", "198.51.100.2", "198.51.100.3"],
            EgressPolicy(allow_private=True),
        )
        backend = _PinnedNetworkBackend(_Burning())
        with pytest.raises(OSError):
            await backend.connect_tcp("budget.example", 443, timeout=0.05)
        # The first attempt spends the budget; the rest are not given a fresh
        # one, so they are not made at all.
        assert len(seen) == 1, f"each address got its own timeout: {seen}"

    async def test_the_fass_name_is_pinned_under_the_spelling_that_is_dialled(self) -> None:
        """``str.encode("idna")`` is not what this stack dials.

        It is the 2003 encoding: it maps ``faß.example`` to ``fass.example``
        while httpx and anyio apply the 2008/UTS-46 rule and send
        ``xn--fa-hia.example``. A pin under the wrong spelling is never found,
        which is the same failure as having no pin at all.
        """
        from cloud_agent.mcp.net_policy import check_addresses, dial_spellings, pinned_addresses
        from cloud_agent.mcp.transports import _PinnedNetworkBackend

        check_addresses("faß.example", ["198.51.100.4"], EgressPolicy(allow_private=True))
        assert "xn--fa-hia.example" in dial_spellings("faß.example")
        assert pinned_addresses("xn--fa-hia.example") == ("198.51.100.4",)

        dialled: list[str] = []

        class _Inner(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Any = None,
            ) -> Any:
                dialled.append(host)
                return None

            async def connect_unix_socket(
                self, path: str, timeout: float | None = None, socket_options: Any = None
            ) -> Any:
                return None

            async def sleep(self, seconds: float) -> None:
                """Nothing to wait for."""

        await _PinnedNetworkBackend(_Inner()).connect_tcp("xn--fa-hia.example", 443)
        assert dialled == ["198.51.100.4"]

    async def test_terminating_a_session_checks_the_address_and_the_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DELETE is the dial most likely to arrive without a pin.

        Sessions are closed by whichever task notices they are superseded, not
        by the one that opened them, so the check has to run in that task --
        and its reply is a body like any other.
        """
        from cloud_agent.mcp import transports as transports_module

        dialled: list[str] = []
        body = _CountingStream(b"x" * 256, 64)

        def handler(request: httpx.Request) -> httpx.Response:
            dialled.append(request.method)
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=body)

        client = _client(handler)
        # The default policy refuses loopback, so nothing may be sent: the
        # check has to come before the DELETE, not after it.
        refused = StreamableHttpTransport(
            _config(session_id="sess-1", policy=EgressPolicy()), client, owns_client=True
        )
        refused.set_era("handshake")
        await refused.terminate()
        assert dialled == [], "an unchecked target was dialled"

        monkeypatch.setattr(transports_module, "MAX_RESPONSE_BYTES", 1024)
        checked = StreamableHttpTransport(_config(session_id="sess-1"), client, owns_client=True)
        checked.set_era("handshake")
        await checked.terminate()
        assert dialled == ["DELETE"]
        # The reply is bounded like every other body: the DELETE is best-effort,
        # which is a reason to stop reading, not a reason to read it all.
        assert body.pulled < 64, "the DELETE's reply was read before the limit applied"
        await client.aclose()

    async def test_the_resolver_is_asked_for_the_name_that_will_be_dialled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolve, check and dial have to name one host.

        ``socket.getaddrinfo`` applies the 2003 IDNA codec, so looking up the
        typed spelling of ``faß.example`` asks about ``fass.example`` -- a
        different domain -- and the connection then dials
        ``xn--fa-hia.example``. The addresses that passed the check would
        belong to a host nobody connects to.
        """
        served: list[int] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                served.append(int(self.server.server_address[1]))
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                """Keep the test output to the test's own assertions."""

        import socketserver

        class _Server(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = _Server(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        asked: list[str] = []
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
            if host == "xn--fa-hia.example":
                asked.append(host)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            if host in ("faß.example", "fass.example"):
                asked.append(host)
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return real_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        config = _config(
            endpoint=normalize_url(f"http://faß.example:{server.server_port}/mcp"),
            policy=EgressPolicy(allow_private=True, allow_plain_http=True),
        )
        client = build_client(config)
        transport = StreamableHttpTransport(config, client, owns_client=True)
        try:
            assert await transport.request("tools/list") == {"ok": True}
        finally:
            await client.aclose()
            server.shutdown()
            server.server_close()
        assert asked == ["xn--fa-hia.example"], f"the resolver was asked about {asked}"
        assert served == [server.server_port]
