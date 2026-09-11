"""Carrying MCP over HTTP: Streamable HTTP, the legacy SSE shape, and metadata.

Two transports share almost everything and differ in one place, so they share
one module and one correlation table.

**Streamable HTTP** sends each request as a POST and reads the answer from
either a JSON body or a short SSE stream, with messages the server initiates
arriving on those same replies and, optionally, on a long-lived GET stream.

**The 2024-11-05 shape** opens a GET stream first, reads an ``endpoint`` event
from it that names the URL requests must be POSTed to, and receives every reply
on the stream rather than on the POST. That announced URL is the reason this
module validates addresses in two places instead of one: a server that
announces ``http://169.254.169.254/`` must be refused at the announcement, not
after the first request has already gone there.

What both share with the rest of the package:

- **The credential is bound to an origin.** It is attached when the request
  goes to the origin it belongs to and never otherwise, so a redirect or an
  announced endpoint on another host is a diagnosis rather than a leak.
- **Nothing is unbounded.** One message, one response, one stream and one wait
  each have a limit, and exceeding one ends the exchange with an error instead
  of growing a buffer.
- **A server request is answered, not ignored.** ``elicitation/create`` and
  ``sampling/createMessage`` arrive while a call is in flight; the handler is
  injected, and a server whose request we cannot answer gets a JSON-RPC error
  rather than a hang.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from cloud_agent.mcp.jsonrpc import (
    MCP_RESERVED_CODES,
    JsonRpcError,
    ProtocolViolation,
    UnsupportedProtocolVersion,
    decode,
    encode_error,
    encode_notification,
    encode_request,
    encode_result,
    id_key,
)
from cloud_agent.mcp.models import PREFERRED_PROTOCOL_VERSION, AuthKind, AuthProfile
from cloud_agent.mcp.net_policy import (
    EgressPolicy,
    EndpointRejected,
    NormalizedEndpoint,
    check_addresses,
    sanitize_headers,
    validate_endpoint,
)
from cloud_agent.mcp.oauth import MetadataFetcher
from cloud_agent.mcp.protocol import (
    Handshake,
    discover_server,
    initialize,
    negotiate_version,
    request_headers,
    request_meta,
)
from cloud_agent.mcp.sse import SseDecoder, SseError

#: A tool call can be a repo-wide search; it is not a token stream. Bounded all
#: the same, because a sandbox waiting on a hung upstream burns a job.
DEFAULT_REQUEST_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 10.0

#: The header carrying the negotiated protocol revision.
PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
#: The header carrying a server-assigned session.
SESSION_ID_HEADER = "mcp-session-id"

#: Response headers a client must act on, and the only ones it reads.
_FORWARDED_RESPONSE_HEADERS = ("content-type", SESSION_ID_HEADER, PROTOCOL_VERSION_HEADER)

#: The largest response body this platform will read into memory.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class TransportError(RuntimeError):
    """Raised when a transport cannot carry a request, with a diagnosis kind."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unreachable",
        status: int | None = None,
        payload: bytes = b"",
    ) -> None:
        """Record the diagnosis kind, any HTTP status and any body.

        The body is kept because era detection has to read it: a 400 carrying a
        recognised MCP error means a modern server, while a 400 with an empty or
        unparseable body means a legacy one. Deciding from the status alone gets
        one of the two wrong.
        """
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.payload = payload

    def modern_error(self) -> JsonRpcError | None:
        """The MCP-reserved error in this reply's body, if there is one."""
        if not self.payload:
            return None
        try:
            messages = decode(self.payload)
        except (JsonRpcError, ProtocolViolation, ValueError):
            return None
        for message in messages:
            if message.is_error:
                error = message.error()
                if error.code in MCP_RESERVED_CODES or isinstance(
                    error, UnsupportedProtocolVersion
                ):
                    return error
        return None


RequestHandler = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class RemoteTransportConfig:
    """Everything one remote transport needs, with the secret already resolved."""

    endpoint: NormalizedEndpoint
    auth: AuthProfile
    policy: EgressPolicy
    #: The static credential or OAuth access token, decrypted by the caller.
    secret: str | None = None
    #: Extra user-configured headers, already sanitized.
    headers: Mapping[str, str] = field(default_factory=dict)
    protocol_version: str = ""
    session_id: str | None = None
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    #: Overrides the scheme check for a private connector bridge. Set only by
    #: the caller that already knows the location.
    allow_plain_http: bool = False


def _auth_headers(config: RemoteTransportConfig) -> dict[str, str]:
    """Build the outbound credential headers for one request."""
    if config.auth.kind is AuthKind.NONE or not config.secret:
        return {}
    if config.auth.kind is AuthKind.STATIC:
        return config.auth.header.render(config.secret)
    return {"Authorization": f"Bearer {config.secret}"}


def merge_headers(base: dict[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """Overlay ``extra`` on ``base``, treating one header name as one name.

    HTTP header names are case-insensitive and ``dict`` keys are not, so a plain
    ``update`` keeps *both* spellings when a connection writes ``authorization``
    and the platform writes ``Authorization``. httpx then sends both, the server
    picks one, and the credential meant to replace the sandbox's token may be
    the one that loses. This is the same merge the relay's proxy performs, and
    for the same reason.
    """
    merged = dict(base)
    for name, value in extra.items():
        for existing in [key for key in merged if key.lower() == name.lower()]:
            del merged[existing]
        merged[name] = value
    return merged


def build_headers(config: RemoteTransportConfig, *, body: bool) -> dict[str, str]:
    """Assemble the header set for one outbound request.

    User headers are applied first and the credential last, so a connection
    cannot configure its way over the credential the platform resolved -- which
    is what would otherwise let a pasted ``Authorization`` header outlive a
    re-authorization.
    """
    headers: dict[str, str] = {
        "accept": "application/json, text/event-stream",
    }
    if body:
        headers["content-type"] = "application/json"
    if config.protocol_version:
        headers[PROTOCOL_VERSION_HEADER] = config.protocol_version
    if config.session_id:
        headers[SESSION_ID_HEADER] = config.session_id
    headers = merge_headers(headers, sanitize_headers(config.headers))
    return merge_headers(headers, _auth_headers(config))


class _HttpCore:
    """Shared request plumbing: address checks, headers, body limits."""

    def __init__(
        self,
        config: RemoteTransportConfig,
        client: httpx.AsyncClient,
    ) -> None:
        """Hold one client for the life of the transport."""
        self._config = config
        self._client = client
        self._session_id = config.session_id
        self._protocol_version = config.protocol_version
        # ``modern`` until a probe says otherwise: the current revision is the
        # one a new server is most likely to speak, and the legacy probe is the
        # fallback rather than the default.
        self._era = "modern"
        self.on_server_request: RequestHandler | None = None

    @property
    def session_id(self) -> str | None:
        """The session the server assigned, if any."""
        return self._session_id

    @property
    def protocol_version(self) -> str:
        """The revision in force for this connection."""
        return self._protocol_version

    def set_protocol_version(self, version: str) -> None:
        """Record the revision the handshake or discovery agreed to."""
        self._protocol_version = version

    def set_era(self, era: str) -> None:
        """Record which shape this connection turned out to use."""
        if era not in ("modern", "handshake"):
            raise ValueError(f"unknown protocol era: {era!r}")
        self._era = era

    @property
    def era(self) -> str:
        """``modern`` for per-request metadata, ``handshake`` for initialize."""
        return self._era

    @property
    def stateless(self) -> bool:
        """Whether this connection carries its version on every request."""
        return self._era == "modern"

    def _config_with_state(self) -> RemoteTransportConfig:
        """The config, with anything learned since construction folded in."""
        return RemoteTransportConfig(
            endpoint=self._config.endpoint,
            auth=self._config.auth,
            policy=self._config.policy,
            secret=self._config.secret,
            headers=self._config.headers,
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            request_timeout=self._config.request_timeout,
            connect_timeout=self._config.connect_timeout,
            allow_plain_http=self._config.allow_plain_http,
        )

    def _capture(self, response: httpx.Response) -> None:
        """Record the session and revision a response assigned."""
        session = response.headers.get(SESSION_ID_HEADER)
        if session:
            self._session_id = session
        version = response.headers.get(PROTOCOL_VERSION_HEADER)
        if version:
            self._protocol_version = version

    async def _guard(self, url: str) -> NormalizedEndpoint:
        """Validate a URL and the addresses it resolves to before dialling it."""
        return await guard_target(url, self._config.policy, origin=self._config.endpoint.origin)

    async def _answer_server_request(self, message: Any) -> bytes | None:
        """Answer a server-initiated request, or refuse it explicitly."""
        if not message.is_request:
            return None
        handler = self.on_server_request
        if handler is None:
            return encode_error(
                message.raw_id,
                JsonRpcError(-32601, f"this platform does not handle {message.method!r}"),
            )
        try:
            result = handler(message.method or "", message.params)
            if asyncio.iscoroutine(result):
                result = await result
        except JsonRpcError as exc:
            return encode_error(message.raw_id, exc)
        except Exception:
            return encode_error(
                message.raw_id, JsonRpcError(-32603, "the platform could not answer")
            )
        return encode_result(message.raw_id, result)

    async def _drain_sse(
        self,
        response: httpx.Response,
        *,
        expect_key: str | None,
        poster: Callable[[bytes], Any] | None = None,
    ) -> list[Any]:
        """Read an SSE reply, returning the result for ``expect_key`` if given."""
        decoder = SseDecoder()
        results: list[Any] = []
        found: Any = None
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise TransportError(
                    "this server sent more data than the platform will read for one request",
                    kind="protocol_incompatible",
                )
            for event in decoder.feed(chunk):
                if not event.data:
                    continue
                try:
                    payload = event.json()
                except SseError:
                    continue
                messages = payload if isinstance(payload, list) else [payload]
                for raw in messages:
                    if not isinstance(raw, Mapping):
                        continue
                    message = decode(json.dumps(raw))[0]
                    if message.is_response:
                        if expect_key is not None and message.key == expect_key:
                            found = message
                        else:
                            results.append(message)
                        continue
                    if self.stateless:
                        # The revision forbids a server from sending requests
                        # of its own; it answers with ``InputRequiredResult``
                        # instead. Answering here would be inventing a
                        # mechanism this server does not have, so the message
                        # is passed to the caller as-is.
                        results.append(message)
                        continue
                    reply = await self._answer_server_request(message)
                    if reply is not None and poster is not None:
                        await poster(reply)
        for event in decoder.close():
            if event.data:
                try:
                    payload = event.json()
                except SseError:
                    continue
                for raw in payload if isinstance(payload, list) else [payload]:
                    if isinstance(raw, Mapping):
                        message = decode(json.dumps(raw))[0]
                        if (
                            message.is_response
                            and expect_key is not None
                            and message.key == expect_key
                        ):
                            found = message
        if found is not None:
            return [found]
        return results


async def guard_target(
    url: str,
    policy: EgressPolicy,
    *,
    origin: str | None = None,
) -> NormalizedEndpoint:
    """Validate a URL and every address it resolves to before anything dials it.

    Module level rather than a method because there is more than one place that
    dials: the transports, and the per-connection relay that forwards a caller's
    raw JSON-RPC. A rule enforced at one of them and not the other is not a
    rule -- the path that skips it becomes the way in.

    ``origin`` binds the target to the one the credential belongs to. It is
    optional because the relay validates a target the registry already chose,
    and required by the transports, which follow URLs a server hands them.
    """
    endpoint = validate_endpoint(url, policy)
    if origin is not None and endpoint.origin != origin:
        raise TransportError(
            f"{endpoint.origin} is not the origin this connection's credential belongs to",
            kind="blocked_target",
        )
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(endpoint.host, endpoint.port, type=0)
        addresses = {info[4][0] for info in infos}
        check_addresses(endpoint.host, addresses, policy)
    except EndpointRejected as exc:
        raise TransportError(str(exc), kind=exc.kind) from exc
    except OSError as exc:
        raise TransportError(f"{endpoint.host} could not be resolved: {exc}") from exc
    return endpoint


class StreamableHttpTransport(_HttpCore):
    """One MCP connection over Streamable HTTP."""

    def __init__(self, config: RemoteTransportConfig, client: httpx.AsyncClient) -> None:
        """Build a transport; :func:`connect_streamable` establishes a session."""
        super().__init__(config, client)
        self._next_id = 0

    def _new_id(self) -> str:
        """Allocate a request id, unique within this connection."""
        self._next_id += 1
        return f"c{self._next_id}"

    def _params_for(self, params: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Add the per-request metadata a stateless-era server requires."""
        if not self.stateless:
            return dict(params) if params is not None else None
        merged: dict[str, Any] = dict(params or {})
        merged["_meta"] = request_meta(self._protocol_version)
        return merged

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Send one request and return its result."""
        request_id = self._new_id()
        request_params = self._params_for(params)
        body = encode_request(request_id, method, request_params)

        async def post(payload: bytes) -> httpx.Response:
            return await self._post(payload, method=method, params=request_params)

        response = await post(body)
        await self._raise_for_status(response, method)
        self._capture(response)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            messages = await self._drain_sse(response, expect_key=id_key(request_id), poster=post)
            for message in messages:
                if message.key == id_key(request_id):
                    return message.result()
            raise TransportError(
                f"the server did not answer {method!r} on the stream it opened",
                kind="protocol_incompatible",
            )
        payload = await response.aread()
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TransportError("this server's reply is larger than the platform will read")
        if response.status_code == 202:
            # Accepted, with no body: the only valid shape for a notification.
            return None
        messages = decode(payload)
        for message in messages:
            if message.is_response and message.key == id_key(request_id):
                return message.result()
        if not messages:
            raise TransportError(
                f"the server sent no reply to {method!r}", kind="protocol_incompatible"
            )
        raise TransportError(
            f"the server's reply to {method!r} did not carry that request's id",
            kind="protocol_incompatible",
        )

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send one notification, which expects no reply.

        A stateless-era server defines no client notifications over HTTP, so
        this is used only where the revision allows it. It is kept because the
        handshake era does use them -- ``notifications/initialized`` above all.
        """
        request_params = dict(params) if params is not None else None
        response = await self._post(
            encode_notification(method, request_params), method=method, params=request_params
        )
        await self._raise_for_status(response, method)
        self._capture(response)
        await response.aread()

    async def _post(
        self,
        payload: bytes,
        *,
        method: str = "",
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """POST one JSON-RPC payload to the endpoint."""
        config = self._config_with_state()
        await self._guard(config.endpoint.url)
        headers = build_headers(config, body=True)
        if self.stateless and method:
            headers.update(request_headers(method, params))
        try:
            return await self._client.post(
                config.endpoint.url,
                content=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"the server could not be reached: {exc}") from exc

    async def _raise_for_status(self, response: httpx.Response, method: str) -> None:
        """Turn an HTTP failure into a diagnosis, without reading the body."""
        if response.status_code in (401, 403):
            raise TransportError(
                "the server rejected this connection's credential; reconnect it",
                kind="needs_authorization",
                status=response.status_code,
            )
        if response.status_code == 404:
            raise TransportError(
                f"the server has no MCP endpoint at that address ({method})",
                kind="unreachable",
                status=404,
            )
        if response.status_code >= 400:
            # The body is read here, unlike on the success path, because era
            # detection and the caller's diagnosis both need it: it is the only
            # thing that distinguishes a modern server refusing a version from
            # a legacy server that has never heard of the request.
            try:
                payload = await response.aread()
            except httpx.HTTPError:
                payload = b""
            raise TransportError(
                f"the server answered {response.status_code} to {method!r}",
                kind="protocol_incompatible",
                status=response.status_code,
                payload=payload,
            )

    async def terminate(self) -> None:
        """End the session, when the server issued one.

        A stateless-era server issues none and answers DELETE with 405, so this
        is a no-op there rather than a request that is expected to fail.
        """
        if not self._session_id or self.stateless:
            return
        config = self._config_with_state()
        try:
            await self._client.delete(
                config.endpoint.url, headers=build_headers(config, body=False)
            )
        except httpx.HTTPError:
            # Best effort: a session that has already timed out upstream is a
            # normal outcome, and failing the caller's disconnect over it would
            # leave a credential in place for a connection it wanted gone.
            return
        finally:
            self._session_id = None


class LegacySseTransport(_HttpCore):
    """One MCP connection over the 2024-11-05 GET-stream plus POST shape."""

    def __init__(
        self,
        config: RemoteTransportConfig,
        client: httpx.AsyncClient,
        *,
        message_url: str | None = None,
    ) -> None:
        """Build a transport, optionally already knowing where requests go."""
        super().__init__(config, client)
        self._message_url = message_url
        self._announcement: asyncio.Future[str] | None = None
        self._next_id = 0
        self._stream_task: asyncio.Task[None] | None = None
        self._inbox: dict[str, Any] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        #: Set when the stream has ended, so a waiter can tell "no reply yet"
        #: from "there is no longer anything that could reply".
        self._ended = False

    def announcement(self) -> asyncio.Future[str]:
        """The message endpoint, resolved when the stream names one.

        A future rather than a value because the announcement arrives on the
        same stream the replies do, and the stream can only be read once: this
        transport owns that reader, so the connection waits on an event it
        resolves instead of opening a second pass over the bytes -- which httpx
        refuses outright, and which a server would never send twice anyway.
        """
        if self._announcement is None:
            self._announcement = asyncio.get_running_loop().create_future()
            if self._message_url:
                self._announcement.set_result(self._message_url)
        return self._announcement

    def announced(self, message_url: str) -> None:
        """Record the endpoint the stream announced, once."""
        self._message_url = message_url
        future = self.announcement()
        if not future.done():
            future.set_result(message_url)

    def _new_id(self) -> str:
        """Allocate a request id."""
        self._next_id += 1
        return f"l{self._next_id}"

    def attach_stream(self, task: asyncio.Task[None]) -> None:
        """Register the background reader for cancellation on close."""
        self._stream_task = task

    def deliver(self, message: Any) -> None:
        """Record a message that arrived on the stream."""
        if message.is_response:
            self._inbox[message.key] = message
            waiter = self._waiters.pop(message.key, None)
            if waiter is not None:
                waiter.set()

    def _waiter(self, key: str) -> asyncio.Event:
        """Get or create the event a request waits on."""
        event = self._waiters.get(key)
        if event is None:
            event = asyncio.Event()
            self._waiters[key] = event
        return event

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Send one request and wait for its reply on the stream."""
        request_id = self._new_id()
        key = id_key(request_id)
        waiter = self._waiter(key)
        config = self._config_with_state()
        # The validated endpoint, not the announcement as it arrived: the
        # raw value is a path until the connection has checked it against the
        # origin and the egress policy, and a request must never go to the
        # unchecked form.
        message_url = self._message_url or await self.announcement()
        await self._guard(message_url)
        try:
            response = await self._client.post(
                message_url,
                content=encode_request(request_id, method, params),
                headers=build_headers(config, body=True),
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"the server could not be reached: {exc}") from exc
        if response.status_code in (401, 403):
            raise TransportError(
                "the server rejected this connection's credential; reconnect it",
                kind="needs_authorization",
                status=response.status_code,
            )
        if response.status_code >= 400:
            # The body is read here, unlike on the success path, because era
            # detection and the caller's diagnosis both need it: it is the only
            # thing that distinguishes a modern server refusing a version from
            # a legacy server that has never heard of the request.
            try:
                payload = await response.aread()
            except httpx.HTTPError:
                payload = b""
            raise TransportError(
                f"the server answered {response.status_code} to {method!r}",
                kind="protocol_incompatible",
                status=response.status_code,
                payload=payload,
            )
        await response.aread()
        try:
            async with asyncio.timeout(config.request_timeout):
                await waiter.wait()
        except TimeoutError as exc:
            self._waiters.pop(key, None)
            raise TransportError(
                f"the server did not answer {method!r} before the timeout",
                kind="unreachable",
            ) from exc
        message = self._inbox.pop(key, None)
        if message is None:
            # A stream that ended is a different diagnosis from a reply that
            # never came, and the caller has a different action for each: one is
            # a server that went away, the other a server that is answering
            # something this connection cannot match.
            if self._ended:
                raise TransportError(
                    f"the server's stream ended before it answered {method!r}",
                    kind="unreachable",
                )
            raise TransportError(
                f"the server's reply to {method!r} was lost", kind="protocol_incompatible"
            )
        return message.result()

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send one notification on the message endpoint."""
        config = self._config_with_state()
        message_url = self._message_url or await self.announcement()
        await self._guard(message_url)
        try:
            response = await self._client.post(
                message_url,
                content=encode_notification(method, params),
                headers=build_headers(config, body=True),
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"the server could not be reached: {exc}") from exc
        await response.aread()

    async def consume(self, response: httpx.Response) -> None:
        """Read the GET stream until it ends, routing everything that arrives."""
        decoder = SseDecoder()
        try:
            async for chunk in response.aiter_bytes():
                for event in decoder.feed(chunk):
                    await self._handle_event(event)
            for event in decoder.close():
                await self._handle_event(event)
        finally:
            # Every waiter is released on stream end. Leaving one blocked would
            # hold a job's request open until its own timeout for a connection
            # that is already gone. Releasing them is not the same as answering
            # them, which is why the flag is set before they wake.
            self._ended = True
            for waiter in list(self._waiters.values()):
                waiter.set()
            await response.aclose()

    async def _handle_event(self, event: Any) -> None:
        """Route one stream event, including the announcement that opens the flow."""
        if event.event == "endpoint" and event.data:
            self.announced(event.data.strip())
            return
        if not event.data:
            return
        try:
            payload = event.json()
        except SseError:
            return
        for raw in payload if isinstance(payload, list) else [payload]:
            if not isinstance(raw, Mapping):
                continue
            message = decode(json.dumps(raw))[0]
            if message.is_response:
                self.deliver(message)
                continue
            reply = await self._answer_server_request(message)
            if reply is not None:
                await self._post_raw(reply)

    async def _post_raw(self, payload: bytes) -> None:
        """POST a pre-encoded payload, used for replying to server requests."""
        config = self._config_with_state()
        try:
            message_url = self._message_url or await self.announcement()
            response = await self._client.post(
                message_url, content=payload, headers=build_headers(config, body=True)
            )
            await response.aread()
        except httpx.HTTPError:
            return

    async def aclose(self) -> None:
        """Stop the background reader."""
        task = self._stream_task
        self._stream_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                return


@dataclass(frozen=True)
class LegacyStream:
    """An opened legacy stream: its transport and the task reading it."""

    transport: LegacySseTransport
    task: asyncio.Task[None]


def build_client(config: RemoteTransportConfig) -> httpx.AsyncClient:
    """Build the HTTP client a transport dials with.

    Redirects are not followed, deliberately: a redirect re-sends this
    connection's credential to whatever host the response names, and the
    legitimate case for one -- a canonical MCP URL -- is rare enough that
    reporting it as a misconfiguration costs less than getting it wrong.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.request_timeout, connect=config.connect_timeout),
        follow_redirects=False,
    )


async def connect_streamable(
    config: RemoteTransportConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[StreamableHttpTransport, Handshake]:
    """Open an HTTP connection, detecting which protocol era the server speaks.

    The two eras cannot be told apart from the address, and sending the wrong
    one produces an error that reads like the server's fault. So the current
    revision is tried first, and what comes back decides:

    - a discovery result, or any MCP-reserved error code, means a modern server;
      an unsupported-version error is retried once with a version it advertises;
    - a rejected request whose body carries no readable MCP error means a server
      that predates the per-request shape, so the handshake is used instead;
    - a plain ``method not found`` means the same thing, whatever status carried
      it. A server that has never heard of ``server/discover`` says so with the
      ordinary JSON-RPC code, and treating that as a modern refusal would fail
      the connection against every server the handshake era actually covers.

    A server that answers none of these ways is reported with the error from the
    attempt that got furthest, rather than with a guess about which era it is.
    """
    owned = client is None
    http = client or build_client(config)
    transport = StreamableHttpTransport(config, http)
    transport.set_era("modern")
    transport.set_protocol_version(config.protocol_version or PREFERRED_PROTOCOL_VERSION)
    try:
        try:
            handshake = await discover_server(transport)
        except UnsupportedProtocolVersion as exc:
            if not exc.supported:
                raise
            transport.set_protocol_version(negotiate_version(exc.supported))
            handshake = await discover_server(transport)
        except TransportError as exc:
            # Over HTTP an unsupported version arrives as a 400 whose *body*
            # carries the code, so the status check has already turned it into a
            # transport error by the time it could be recognised. Read the body
            # back: the retry is the whole reason the server is told which
            # revisions it does implement.
            error = exc.modern_error()
            if not isinstance(error, UnsupportedProtocolVersion) or not error.supported:
                raise
            transport.set_protocol_version(negotiate_version(error.supported))
            handshake = await discover_server(transport)
        transport.set_protocol_version(handshake.protocol_version)
        return transport, handshake
    except TransportError as exc:
        if exc.modern_error() is not None:
            # A modern server answered and refused this particular request.
            # Falling back would ask it to initialize, which it removed.
            if owned:
                await http.aclose()
            raise
        modern_error = exc
    except JsonRpcError as exc:
        if exc.code in MCP_RESERVED_CODES:
            # An MCP-reserved code over a success status is still a modern
            # server describing this request, not a legacy one failing to
            # understand it.
            if owned:
                await http.aclose()
            raise
        # Anything else is the pre-stateless way of saying "no such method".
        modern_error = TransportError(str(exc), kind="protocol_incompatible")
    except BaseException:
        if owned:
            await http.aclose()
        raise

    transport.set_era("handshake")
    transport.set_protocol_version("")
    try:
        handshake = await initialize(transport)
    except BaseException:
        if owned:
            await http.aclose()
        raise
    transport.set_protocol_version(handshake.protocol_version)
    del modern_error
    return transport, handshake


async def connect_legacy_sse(
    config: RemoteTransportConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[LegacySseTransport, Handshake, asyncio.Task[None]]:
    """Open a legacy HTTP+SSE connection: read the announcement, then handshake.

    The announced POST URL is validated against the same egress policy and the
    same origin as the endpoint that announced it, because the announcement is
    exactly the point where a server can redirect the credential somewhere
    else.
    """
    http = client or build_client(config)
    try:
        stream_request = http.build_request(
            "GET",
            config.endpoint.url,
            headers={
                **build_headers(config, body=False),
                "accept": "text/event-stream",
            },
        )
        response = await http.send(stream_request, stream=True)
    except httpx.HTTPError as exc:
        await http.aclose()
        raise TransportError(f"the server could not be reached: {exc}") from exc

    # The stream is read exactly once, by the transport, because httpx will not
    # hand the same response body to a second reader -- and because reading it
    # here and again there is how the bytes between the two passes go missing.
    transport = LegacySseTransport(config, http)
    stream_task = asyncio.create_task(transport.consume(response))
    transport.attach_stream(stream_task)
    try:
        async with asyncio.timeout(config.connect_timeout * 3):
            announced = await transport.announcement()
    except TimeoutError as exc:
        await transport.aclose()
        await http.aclose()
        raise TransportError(
            "the server never announced a message endpoint on its stream",
            kind="protocol_incompatible",
        ) from exc

    # Validated before anything is sent to it: the announcement is precisely
    # where a server can point this connection's credential somewhere else.
    transport.announced(_resolve_message_url(config, announced))
    try:
        handshake = await initialize(transport)
    except BaseException:
        await transport.aclose()
        await http.aclose()
        raise
    transport.set_protocol_version(handshake.protocol_version)
    return transport, handshake, stream_task


def _resolve_message_url(config: RemoteTransportConfig, announced: str) -> str:
    """Resolve a possibly relative announcement against the endpoint."""
    if announced.startswith("http://") or announced.startswith("https://"):
        candidate = announced
    else:
        base = config.endpoint.url
        joined = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
        candidate = httpx.URL(joined).join(announced).__str__()
    endpoint = validate_endpoint(candidate, config.policy)
    if endpoint.origin != config.endpoint.origin:
        # The specification allows a server to announce a different origin;
        # this platform does not follow it, because following it would spend
        # the user's credential on a host they never approved.
        raise TransportError(
            f"the server announced {endpoint.origin} for its messages, which is not the "
            "origin this connection's credential belongs to",
            kind="blocked_target",
        )
    return endpoint.url


@dataclass
class HttpMetadataFetcher(MetadataFetcher):
    """Fetch OAuth metadata documents under the same egress policy.

    Deliberately not a second HTTP client with its own settings: the policy,
    the redirect refusal and the size limit are the same ones the MCP endpoint
    gets, because every hole in this feature comes from a second code path that
    was written later and checks less.
    """

    client: httpx.AsyncClient | None = None
    timeout: float = DEFAULT_CONNECT_TIMEOUT * 2

    async def fetch_json(  # type: ignore[override]
        self,
        url: str,
        *,
        policy: EgressPolicy,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        """Fetch and decode one JSON document."""
        endpoint = validate_endpoint(url, policy)
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(endpoint.host, endpoint.port, type=0)
            check_addresses(endpoint.host, {info[4][0] for info in infos}, policy)
        except EndpointRejected as exc:
            raise TransportError(str(exc), kind=exc.kind) from exc
        owned = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False
        )
        try:
            response = await client.get(
                endpoint.url,
                headers={"accept": "application/json", **dict(headers or {})},
            )
            if response.status_code >= 400:
                raise TransportError(
                    f"{endpoint.origin} answered {response.status_code} for its metadata",
                    kind="unreachable",
                    status=response.status_code,
                )
            payload = await response.aread()
        except httpx.HTTPError as exc:
            raise TransportError(f"{endpoint.origin} could not be reached: {exc}") from exc
        finally:
            if owned:
                await client.aclose()
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TransportError("that metadata document is larger than the platform will read")
        try:
            document = json.loads(payload or b"{}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise TransportError(f"that metadata document is not JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            raise TransportError("that metadata document is not a JSON object")
        return document, dict(response.headers.items())


async def iter_sse(response: httpx.Response) -> AsyncIterator[Any]:
    """Yield decoded events from a streaming response, for callers that want them."""
    decoder = SseDecoder()
    async for chunk in response.aiter_bytes():
        for event in decoder.feed(chunk):
            yield event
    for event in decoder.close():
        yield event


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_REQUEST_TIMEOUT",
    "MAX_RESPONSE_BYTES",
    "PROTOCOL_VERSION_HEADER",
    "SESSION_ID_HEADER",
    "HttpMetadataFetcher",
    "LegacySseTransport",
    "LegacyStream",
    "RemoteTransportConfig",
    "StreamableHttpTransport",
    "TransportError",
    "build_client",
    "build_headers",
    "connect_legacy_sse",
    "connect_streamable",
    "guard_target",
    "iter_sse",
    "merge_headers",
]
