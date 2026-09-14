"""The protocol host: what the relay drives to reach one server.

The relay used to be a forwarder. It took a JSON-RPC body from a sandbox, put
the owner's credential on it and posted it upstream, because the *runtime* owned
the protocol: the CLI spoke MCP, and the relay was a pipe with a filter in it.

That only works while every server speaks the shape the CLI speaks. A generic
connection can be a legacy SSE server, a stateless-era server that refuses
``initialize``, a stdio program, or a server that answers a tool call with
``InputRequiredResult`` and expects the caller to come back with the answers.
None of those are a pipe. So the platform speaks the protocol itself and
presents one surface to the runtimes -- which is also what makes "all five
coding agents" a statement about adapters rather than about five separate
integrations.

This module is that surface. It owns:

- **Connecting**, including which era the server turned out to speak, and the
  session record that ties a live protocol session to one member and one owner
  lease generation.
- **Discovery**, turning what a server advertises into the snapshot the
  authorization layer reads.
- **Calling**, including the multi-round-trip continuation, and normalizing a
  result into the bounded, honest shape the runtimes render.
- **Refusing stdio it cannot run.** A ``managed-worker`` or ``user-connector``
  server needs a worker or a connector, and neither is a Python object in this
  process. Rather than pretending, the host raises and says which component is
  missing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakValueDictionary

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import protocol
from cloud_agent.mcp.jsonrpc import JsonRpcError
from cloud_agent.mcp.models import (
    CapabilitySnapshot,
    ExecutionLocation,
    McpConnection,
    Transport,
)
from cloud_agent.mcp.net_policy import EgressPolicy
from cloud_agent.mcp.transports import (
    DEFAULT_REQUEST_TIMEOUT,
    RemoteTransportConfig,
    TransportError,
    connect_legacy_sse,
    connect_streamable,
)

logger = get_logger(__name__)

#: A single text or base64 block the runtimes will render. Bounded because a
#: tool result is on its way into a model's context, and an unbounded one is a
#: job that dies of context exhaustion three calls later.
MAX_CONTENT_BYTES = 512 * 1024
MAX_CONTENT_BLOCKS = 64
SESSION_CLOSE_TIMEOUT_S = 2.0
SESSION_CHECK_TIMEOUT_S = 5.0
_CLEANUP_CONCURRENCY = 8

#: Content types the platform can carry through unchanged.
_RENDERABLE_BLOCKS = frozenset({"text", "image", "audio", "resource_link", "resource"})


class HostError(RuntimeError):
    """Raised when a host operation cannot proceed, with a diagnosis kind."""

    def __init__(self, message: str, *, kind: str = "unreachable") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


class ConnectorRequired(HostError):
    """Raised for a stdio connection with no worker or connector behind it."""

    def __init__(self, connection_id: str, location: ExecutionLocation) -> None:
        """Name the connection and the component that would have to run it."""
        super().__init__(
            f"connection {connection_id} runs a local process, which needs a "
            f"{'managed worker' if location is ExecutionLocation.MANAGED_WORKER else 'user connector'} "
            "that this deployment has not been given",
            kind="launch_failed",
        )
        self.connection_id = connection_id
        self.location = location


@dataclass(frozen=True)
class ContentBlock:
    """One normalized content block from a tool result."""

    type: str
    text: str = ""
    data: str = ""
    mime_type: str = ""
    uri: str = ""
    #: Set when the block was shortened, so a model is told rather than misled.
    truncated: bool = False
    resource: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Render for the runtime."""
        payload: dict[str, Any] = {**self.metadata, "type": self.type}
        if self.type == "text":
            payload["text"] = self.text
        if self.type in {"image", "audio"}:
            payload["data"] = self.data
            payload["mimeType"] = self.mime_type
        if self.type == "resource":
            payload["resource"] = dict(self.resource or {})
        if self.type == "resource_link":
            payload["uri"] = self.uri
            payload.setdefault("name", self.uri)
        if self.mime_type and self.type != "resource":
            payload["mimeType"] = self.mime_type
        if self.uri and self.type != "resource":
            payload["uri"] = self.uri
        if self.truncated:
            payload["truncated"] = True
        return payload


@dataclass(frozen=True)
class ToolResult:
    """A normalized ``tools/call`` result.

    ``unsupported`` is the honest part. A content block type this platform
    cannot represent is named rather than dropped: a tool that returns an
    image to a runtime with no image support has still returned something, and
    reporting an empty result would tell the model the call succeeded with
    nothing in it.
    """

    content: tuple[ContentBlock, ...] = ()
    structured: Any = None
    is_error: bool = False
    unsupported: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Render for the relay's reply and for event payloads."""
        payload: dict[str, Any] = {
            "content": [block.as_dict() for block in self.content],
            "isError": self.is_error,
        }
        if self.structured is not None:
            payload["structuredContent"] = self.structured
        if self.unsupported:
            payload["unsupportedContent"] = list(self.unsupported)
        return payload


@dataclass(frozen=True)
class SessionKey:
    """Which member owns one live protocol session.

    ``member`` is the job-and-attempt identity the session belongs to. A
    session is never shared between members: two turns of one conversation are
    two members, and handing the second the first's protocol session would leak
    the first's upstream state across an attempt boundary the platform treats
    as a security boundary everywhere else.
    """

    connection_id: str
    member: str
    owner_lease_generation: int = 0

    def handle(self) -> str:
        """A stable opaque handle for this member's session."""
        digest = hashlib.sha256(
            f"{self.connection_id}\x00{self.member}\x00{self.owner_lease_generation}".encode()
        ).hexdigest()
        return f"mcp1.{digest[:32]}"

    @property
    def job_attempt(self) -> tuple[str, str]:
        """Split the member back into its job and attempt parts."""
        job, _, attempt = self.member.partition(":")
        return job, attempt

    def supersedes(self, other: SessionKey) -> bool:
        """Whether this key replaces an older attempt or lease of the same job."""
        if self.connection_id != other.connection_id:
            return False
        if self.member == other.member:
            return self.owner_lease_generation > other.owner_lease_generation
        job, attempt = self.job_attempt
        other_job, other_attempt = other.job_attempt
        return (
            job == other_job
            and attempt.isdecimal()
            and other_attempt.isdecimal()
            and int(attempt) > int(other_attempt)
        )


@dataclass
class Session:
    """One open protocol session, with the transport that carries it."""

    key: SessionKey
    transport: Any
    handshake: protocol.Handshake
    snapshot: CapabilitySnapshot | None = None
    #: The legacy stream reader, when the transport has one, so it can be
    #: cancelled on close instead of leaking a task per reconnection.
    stream_task: Any = None
    #: Server requests that arrived with no handler, kept so a caller can see
    #: them rather than have them silently dropped.
    pending_questions: list[dict[str, Any]] = field(default_factory=list)
    _active_operations: int = field(default=0, init=False, repr=False)
    _retired: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def protocol_version(self) -> str:
        """The revision this session agreed."""
        return self.handshake.protocol_version

    @property
    def reusable(self) -> bool:
        """Whether a later call can still use this transport."""
        return (
            not self._closed
            and not self._retired
            and (self.stream_task is None or not self.stream_task.done())
        )

    async def aclose(self) -> None:
        """Finish bounded cleanup even when the disconnecting caller is cancelled."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise

    async def _cleanup(self) -> None:
        """Bound graceful termination separately from releasing local resources."""
        self._closed = True
        try:
            for method in ("terminate", "aclose"):
                operation = getattr(self.transport, method, None)
                if operation is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        async with asyncio.timeout(SESSION_CLOSE_TIMEOUT_S):
                            await operation()
        finally:
            if self.stream_task is not None:
                self.stream_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    async with asyncio.timeout(SESSION_CLOSE_TIMEOUT_S):
                        await self.stream_task

    async def retire(self) -> None:
        """Stop reuse after rotation while letting already dispatched calls finish."""
        self._retired = True
        if not self._active_operations:
            await self.aclose()

    @contextlib.asynccontextmanager
    async def using(self) -> AsyncIterator[None]:
        """Keep the transport alive and expose upstream failures as host errors."""
        if self._closed or self._retired:
            raise HostError("the upstream session was replaced; start a new request")
        self._active_operations += 1
        try:
            yield
        except TransportError as exc:
            self._retired = True
            raise HostError(str(exc), kind=exc.kind) from exc
        except protocol.ProtocolError as exc:
            self._retired = True
            raise HostError(str(exc), kind="protocol_incompatible") from exc
        except JsonRpcError as exc:
            raise HostError(str(exc), kind="protocol_incompatible") from exc
        finally:
            self._active_operations -= 1
            if self._retired and not self._active_operations:
                await self.aclose()


@dataclass(frozen=True)
class HostLimits:
    """Bounds applied to everything a server can send back."""

    max_content_bytes: int = MAX_CONTENT_BYTES
    max_content_blocks: int = MAX_CONTENT_BLOCKS
    max_continuations: int = 3


@dataclass(frozen=True)
class SessionFingerprint:
    """What a live session was opened with, for deciding whether it still fits.

    None of this can live in the key: the key is a digest of the connection, the
    member and the owner lease generation, and every field here changes while
    that key stays the same. So the host records the fingerprint and compares it
    on the way back in, because a cache that answers anyway drives a transport
    opened for something else -- an old credential, an old bound, or an old
    *target*.

    ``config_revision`` is the target: a connection's URL, transport, launch spec
    and authentication profile all move it, and none of them touch
    ``credential_version``. ``authorization_epoch`` moves when the authorization
    behind the credential is replaced. Both matter here and neither is a secret,
    which is why they are compared rather than assumed.
    """

    secret: str | None
    request_timeout_s: float | None
    credential_version: int
    config_revision: int
    authorization_epoch: int

    @classmethod
    def of(cls, connection: McpConnection, *, secret: str | None) -> SessionFingerprint:
        """Read the fingerprint of one connection as a caller offers it."""
        return cls(
            secret=secret,
            request_timeout_s=connection.request_timeout_s,
            credential_version=connection.credential_version,
            config_revision=connection.config_revision,
            authorization_epoch=connection.authorization_epoch,
        )


class McpHost:
    """Opens sessions and drives the protocol for one connection at a time.

    Stateless between calls except for the sessions it holds: a caller asks for
    a session by key, and gets the same one back while the key still matches.
    That is what keeps a stateless-era server's ``subscriptions/listen`` stream
    -- or a legacy server's GET stream -- from being reopened on every call.
    """

    def __init__(
        self,
        *,
        policy: EgressPolicy | None = None,
        limits: HostLimits | None = None,
        store: Any | None = None,
    ) -> None:
        """Build a host with an egress policy, result bounds and a session record.

        ``store`` is optional because a host is also useful without a database --
        a diagnostic opens one connection and closes it. With a store, every
        session it opens is *recorded*: the row names the connection, the member,
        the owner lease generation and the revisions the session was opened
        under, which is what lets a superseded owner's session be found and
        closed. The record is not the session: the socket cannot be handed back
        by a table, and no attempt is made to pretend otherwise.
        """
        self._policy = policy or EgressPolicy()
        self._limits = limits or HostLimits()
        self._store = store
        self._sessions: dict[str, Session] = {}
        self._closed = False
        #: What each live session was opened with, so a caller that arrives with
        #: a rotated token, a changed timeout or a repointed connection is not
        #: silently handed the old transport. Kept beside the sessions rather
        #: than on them because it is the *caller's* state, not the protocol's.
        self._opened_with: dict[str, SessionFingerprint] = {}
        self._session_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            WeakValueDictionary()
        )

    def _session_lock(self, key: SessionKey) -> asyncio.Lock:
        """Serialize lifecycle changes for one member, without blocking other members."""
        job, attempt = key.job_attempt
        identity = (key.connection_id, job if attempt.isdecimal() else key.member)
        lock = self._session_locks.get(identity)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[identity] = lock
        return lock

    def session(self, key: SessionKey) -> Session | None:
        """The live session for one member, if any."""
        return self._sessions.get(key.handle())

    def forget(self, key: SessionKey) -> None:
        """Drop a session record without closing it, for a superseded owner."""
        self._sessions.pop(key.handle(), None)
        self._opened_with.pop(key.handle(), None)

    async def open(
        self,
        connection: McpConnection,
        *,
        secret: str | None,
        key: SessionKey,
    ) -> Session:
        """Open one cached session, sharing a concurrent first connection attempt."""
        async with self._session_lock(key):
            return await self._open(connection, secret=secret, key=key)

    async def _open(
        self, connection: McpConnection, *, secret: str | None, key: SessionKey
    ) -> Session:
        """Open a protocol session, replacing any session with the same key.

        A replacement is deliberate rather than a reuse: the key includes the
        owner lease generation, so a takeover produces a *new* key and leaves
        the old session to be reaped. Reusing it would let a worker whose lease
        expired keep driving a connection the platform has already reassigned.
        """
        if self._closed:
            raise HostError("the MCP host is shutting down")
        if any(session.key.supersedes(key) for session in self._sessions.values()):
            raise HostError("this MCP attempt has already been superseded")
        existing = self._sessions.get(key.handle())
        if existing is not None and self._still_describes(existing, connection, secret):
            return existing
        if existing is not None:
            # The key is unchanged -- same connection, same member, same owner --
            # but something the session was *opened with* is not. A refreshed
            # token, a widened timeout and a repointed connection all arrive
            # under the same key, and a cache that answers with the old transport
            # sends the old credential to the old address under the old bound,
            # none of which the caller asked for.
            del self._sessions[key.handle()]
            previous = self._opened_with.pop(key.handle(), None)
            if (
                previous is not None
                and previous.config_revision == connection.config_revision
                and previous.authorization_epoch == connection.authorization_epoch
            ):
                await existing.retire()
            else:
                # A new target or revoked authorization must close the old
                # session; only routine credential refresh drains in-flight calls.
                await existing.aclose()
        await self._reap_superseded(key)
        transport, handshake, stream_task = await self._connect(connection, secret=secret)
        session = Session(
            key=key, transport=transport, handshake=handshake, stream_task=stream_task
        )
        if self._closed:
            await session.aclose()
            raise HostError("the MCP host is shutting down")
        self._sessions[key.handle()] = session
        self._opened_with[key.handle()] = SessionFingerprint.of(connection, secret=secret)
        try:
            await self._record(connection, session)
        except BaseException:
            self._sessions.pop(key.handle(), None)
            self._opened_with.pop(key.handle(), None)
            await session.aclose()
            raise
        return session

    def _still_describes(
        self, session: Session, connection: McpConnection, secret: str | None
    ) -> bool:
        """Whether a cached session was opened with what the caller just passed.

        Compared rather than assumed, because the key carries none of it: the
        credential is a secret the caller holds for this call, the timeout is a
        property of the connection row, and the revisions are how a connection
        says its target or its authorization moved. Leaving the revisions out is
        what let a session opened against one address keep answering after the
        owner repointed the connection -- an update that deliberately does not
        move ``credential_version``, so the two fields that would have caught it
        are the ones that have to be compared.
        """
        if not session.reusable:
            return False
        recorded = self._opened_with.get(session.key.handle())
        if recorded is None:
            return False
        return recorded == SessionFingerprint.of(connection, secret=secret)

    async def reap_superseded(self, key: SessionKey) -> int:
        """Close older owners after any concurrent connection attempt settles."""
        async with self._session_lock(key):
            return await self._reap_superseded(key)

    async def _reap_superseded(self, key: SessionKey) -> int:
        """Close what this member held under an older owner, and forget it.

        The design's "rebuild the protocol session when the owner is superseded"
        is this and only this. A takeover produces a new key -- the lease
        generation is part of it -- so the old session is a socket the platform
        will never drive again: it is closed here, its record removed, and any
        record another instance left under the same member goes with it. Left
        alone, the map grows for the lifetime of the process and a superseded
        owner stays one dictionary lookup away from being handed its own session
        back.
        """
        closed = 0
        for handle, session in list(self._sessions.items()):
            if key.supersedes(session.key):
                del self._sessions[handle]
                self._opened_with.pop(handle, None)
                await session.aclose()
                closed += 1
                if self._store is not None:
                    await self._store.close_mcp_transport_session(handle=handle)
        if self._store is not None:
            await self._store.reap_mcp_transport_sessions(
                connection_id=int(key.connection_id),
                member=key.member,
                keep_handle=key.handle(),
            )
        return closed

    async def close(self, key: SessionKey) -> bool:
        """Close one session and remove its record."""
        async with self._session_lock(key):
            return await self._close(key)

    async def reap_inactive(self, is_live: Callable[[SessionKey], Awaitable[bool]]) -> int:
        """Release ended members without closing a concurrently replaced session."""

        async def reap(session: Session) -> int:
            try:
                async with asyncio.timeout(SESSION_CHECK_TIMEOUT_S):
                    live = await is_live(session.key)
                if live and session.reusable:
                    return 0
                async with asyncio.timeout(SESSION_CHECK_TIMEOUT_S):
                    async with self._session_lock(session.key):
                        if self.session(session.key) is not session:
                            return 0
                        await self._close(session.key)
                return 1
            except Exception:
                # A failed authority lookup is not evidence of revocation.
                logger.warning("agent_mcp_session_reap_failed", exc_info=True)
                return 0

        sessions = list(self._sessions.values())
        closed = 0
        for offset in range(0, len(sessions), _CLEANUP_CONCURRENCY):
            closed += sum(
                await asyncio.gather(
                    *(reap(session) for session in sessions[offset : offset + _CLEANUP_CONCURRENCY])
                )
            )
        return closed

    async def aclose(self) -> None:
        """Stop accepting sessions and release every remaining upstream socket."""
        self._closed = True
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._opened_with.clear()
        for offset in range(0, len(sessions), _CLEANUP_CONCURRENCY):
            await asyncio.gather(
                *(session.aclose() for session in sessions[offset : offset + _CLEANUP_CONCURRENCY])
            )

    async def _close(self, key: SessionKey) -> bool:
        """Close a session while holding its lifecycle lock."""
        session = self._sessions.pop(key.handle(), None)
        self._opened_with.pop(key.handle(), None)
        if session is not None:
            await session.aclose()
        if self._store is not None:
            return bool(await self._store.close_mcp_transport_session(handle=key.handle()))
        return session is not None

    async def _record(self, connection: McpConnection, session: Session) -> None:
        """Write the session's record, if this host has somewhere to write it.

        A record that cannot be written does not fail the call it describes. The
        socket is process-local, so what is lost is the ability to *find* this
        session from elsewhere -- a diagnostic says less, and a reaper on another
        instance has one fewer row to clean. Nothing about the call's safety
        depends on it, which is exactly why it must not take the call down.
        """
        if self._store is None:
            return
        job_id, _, attempt = session.key.member.partition(":")
        try:
            await self._store.open_mcp_transport_session(
                handle=session.key.handle(),
                connection_id=int(session.key.connection_id),
                member=session.key.member,
                job_id=job_id or None,
                attempt_id=int(attempt) if attempt.isdigit() else None,
                owner_lease_generation=session.key.owner_lease_generation,
                protocol_version=session.protocol_version,
                upstream_session_id=getattr(session.transport, "session_id", None),
                config_revision=connection.config_revision,
                authorization_epoch=connection.authorization_epoch,
            )
        except Exception:
            logger.warning(
                "agent_mcp_session_record_failed",
                extra={
                    "event": "agent_mcp_session_record_failed",
                    "connection_id": session.key.connection_id,
                    "member": session.key.member,
                },
                exc_info=True,
            )

    async def _connect(
        self, connection: McpConnection, *, secret: str | None
    ) -> tuple[Any, protocol.Handshake, Any]:
        """Build and connect the transport one connection's config implies."""
        config = connection.config
        if config.transport is Transport.STDIO:
            raise ConnectorRequired(connection.connection_id, config.location)
        if not config.url:
            raise HostError("this connection has no address", kind="unreachable")

        from cloud_agent.mcp.net_policy import normalize_url

        endpoint = normalize_url(config.url)
        remote = RemoteTransportConfig(
            endpoint=endpoint,
            auth=connection.auth,
            policy=self._policy,
            secret=secret,
            headers={},
            protocol_version="",
            request_timeout=connection.request_timeout_s or DEFAULT_REQUEST_TIMEOUT,
            allow_plain_http=self._policy.allow_plain_http,
        )
        try:
            if config.transport is Transport.HTTP_SSE:
                legacy_transport, handshake, task = await connect_legacy_sse(remote)
                return legacy_transport, handshake, task
            transport, handshake = await connect_streamable(remote)
            return transport, handshake, None
        except TransportError as exc:
            raise HostError(str(exc), kind=exc.kind) from exc
        except (JsonRpcError, protocol.ProtocolError) as exc:
            raise HostError(str(exc), kind="protocol_incompatible") from exc

    async def discover(
        self,
        connection: McpConnection,
        *,
        secret: str | None,
        key: SessionKey,
        capability_revision: int,
    ) -> tuple[Session, CapabilitySnapshot]:
        """Open a session if needed and read everything the server offers."""
        session = await self.open(connection, secret=secret, key=key)
        async with session.using():
            snapshot = await protocol.discover(
                session.transport,
                session.handshake,
                connection_id=connection.connection_id,
                config_revision=connection.config_revision,
                capability_revision=capability_revision,
            )
        session.snapshot = snapshot
        return session, snapshot

    async def call_tool(
        self,
        session: Session,
        *,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        input_responses: Mapping[str, Any] | None = None,
        request_state: str | None = None,
    ) -> ToolResult:
        """Call a tool without closing its transport during concurrent credential rotation."""
        async with session.using():
            return await self._call_tool(
                session,
                name=name,
                arguments=arguments,
                input_responses=input_responses,
                request_state=request_state,
            )

    async def _call_tool(
        self,
        session: Session,
        *,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        input_responses: Mapping[str, Any] | None = None,
        request_state: str | None = None,
    ) -> ToolResult:
        """Call one tool, answering a multi-round-trip request when asked.

        The continuation loop is bounded. A server that keeps asking for input
        is either broken or being driven by something that will never satisfy
        it, and either way the platform stops rather than looping until the
        job's wall clock runs out.
        """
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments or {})}
        responses = dict(input_responses or {})
        state = request_state
        for _ in range(self._limits.max_continuations + 1):
            if responses or state is not None:
                params = protocol.continuation_params(
                    input_responses=responses,
                    request_state=state,
                    base={"name": name, "arguments": dict(arguments or {})},
                )
            result = await session.transport.request(protocol.TOOLS_CALL, params)
            if not protocol.input_required(result):
                return self.render_result(result)
            requests = protocol.input_requests(result)
            answered = await self._answer_input_requests(requests)
            if answered is None:
                raise HostError(
                    "the server asked for input this platform cannot supply",
                    kind="protocol_incompatible",
                )
            responses = answered
            # Echoed verbatim from the result that carried it, never invented.
            raw_state = result.get("requestState") if isinstance(result, Mapping) else None
            state = str(raw_state) if isinstance(raw_state, str) and raw_state else None
        raise HostError(
            f"the server kept asking for input after {self._limits.max_continuations} rounds",
            kind="protocol_incompatible",
        )

    async def _answer_input_requests(self, requests: Mapping[str, Any]) -> dict[str, Any] | None:
        """Answer what this platform can, or report that it cannot.

        Only roots are answerable here, and only with the empty set: the
        platform does not expose a workspace root to a server it is calling,
        because a root is a filesystem claim and this host has no workspace.
        Elicitation and sampling belong to the session's own UI and model
        authorization, which is a caller's job rather than a transport's.
        """
        responses: dict[str, Any] = {}
        for key, request in requests.items():
            if not isinstance(request, Mapping):
                return None
            method = request.get("method")
            if method == protocol.ROOTS_LIST:
                responses[str(key)] = {"roots": []}
                continue
            return None
        return responses

    async def read_resource(self, session: Session, *, uri: str) -> dict[str, Any]:
        """Read one resource, bounded and honestly rendered."""
        async with session.using():
            result = await session.transport.request(protocol.RESOURCES_READ, {"uri": uri})
        if protocol.input_required(result):
            raise HostError(
                "this resource needs input from the user before it can be read",
                kind="protocol_incompatible",
            )
        contents = result.get("contents") if isinstance(result, Mapping) else None
        if not isinstance(contents, list):
            raise HostError("the server returned no content for that resource")
        return {
            "contents": [
                self._render_resource(item, fallback_uri=uri)
                for item in contents[: self._limits.max_content_blocks]
            ],
        }

    async def get_prompt(
        self, session: Session, *, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fetch one prompt template's messages."""
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = dict(arguments)
        async with session.using():
            result = await session.transport.request(protocol.PROMPTS_GET, params)
        if not isinstance(result, Mapping):
            raise HostError("the server's prompt reply is not an object")
        messages = result.get("messages")
        if not isinstance(messages, list):
            raise HostError("the server's prompt reply does not contain messages")
        rendered: list[dict[str, Any]] = []
        for message in messages[: self._limits.max_content_blocks]:
            if not isinstance(message, Mapping) or message.get("role") not in {"user", "assistant"}:
                raise HostError("the server returned an invalid prompt message")
            content = self._render_content(message.get("content"))
            if content is None:
                raise HostError("the server returned unsupported prompt content")
            rendered.append(
                {
                    "role": message["role"],
                    "content": content.as_dict(),
                }
            )
        return {
            "description": str(result.get("description") or ""),
            "messages": rendered,
        }

    def render_result(self, result: Any) -> ToolResult:
        """Normalize a ``tools/call`` result into the bounded shape runtimes see."""
        if not isinstance(result, Mapping):
            return ToolResult(
                content=(ContentBlock(type="text", text=str(result or "")),), is_error=False
            )
        raw_content = result.get("content")
        blocks: list[ContentBlock] = []
        unsupported: list[str] = []
        for item in (raw_content if isinstance(raw_content, list) else [])[
            : self._limits.max_content_blocks
        ]:
            block = self._render_content(item)
            if block is None:
                if isinstance(item, Mapping):
                    unsupported.append(str(item.get("type") or "unknown"))
                continue
            blocks.append(block)
        structured = result.get("structuredContent")
        if structured is not None and self._oversized(json.dumps(structured, ensure_ascii=False)):
            raise HostError("the server returned structured content larger than the platform limit")
        return ToolResult(
            content=tuple(blocks),
            structured=structured,
            is_error=bool(result.get("isError")),
            unsupported=tuple(unsupported),
        )

    def _render_content(self, item: Any) -> ContentBlock | None:
        """Convert one content block, or ``None`` when it cannot be carried."""
        if isinstance(item, str):
            return self._bounded_text(item)
        if not isinstance(item, Mapping):
            return None
        block_type = str(item.get("type") or "")
        if block_type not in _RENDERABLE_BLOCKS:
            return None
        if block_type == "text":
            block = self._bounded_text(str(item.get("text") or ""))
            return ContentBlock(
                type="text",
                text=block.text,
                truncated=block.truncated,
                metadata=self._content_metadata(item),
            )
        if block_type in ("image", "audio"):
            data = str(item.get("data") or "")
            if self._oversized(data):
                raise HostError("the server returned binary content larger than the platform limit")
            return ContentBlock(
                type=block_type,
                data=data,
                mime_type=str(item.get("mimeType") or ""),
                metadata=self._content_metadata(item),
            )
        if block_type == "resource_link":
            return ContentBlock(
                type=block_type,
                uri=str(item.get("uri") or ""),
                mime_type=str(item.get("mimeType") or ""),
                metadata={
                    key: item[key]
                    for key in (
                        "name",
                        "title",
                        "description",
                        "size",
                        "icons",
                        "annotations",
                        "_meta",
                    )
                    if key in item
                },
            )
        resource = item.get("resource")
        if isinstance(resource, Mapping):
            return ContentBlock(
                type="resource",
                resource=self._render_resource(resource),
                metadata=self._content_metadata(item),
            )
        return None

    @staticmethod
    def _content_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
        """Preserve MCP annotations and metadata alongside the normalized content."""
        return {key: item[key] for key in ("annotations", "_meta") if key in item}

    def _render_resource(self, item: Any, *, fallback_uri: str = "") -> dict[str, Any]:
        """Preserve the resource envelope expected by native MCP clients."""
        if not isinstance(item, Mapping):
            raise HostError("the server returned invalid resource content")
        resource = dict(item)
        resource["uri"] = str(item.get("uri") or fallback_uri)
        if isinstance(item.get("text"), str):
            bounded = self._bounded_text(item["text"])
            resource["text"] = bounded.text
            if bounded.truncated:
                resource["truncated"] = True
        elif isinstance(item.get("blob"), str):
            if self._oversized(item["blob"]):
                raise HostError("the server returned binary content larger than the platform limit")
        else:
            raise HostError("the server returned resource content without text or a blob")
        return resource

    def _bounded_text(self, text: str, *, uri: str = "") -> ContentBlock:
        """Truncate text at the block bound, saying so when it did."""
        limit = self._limits.max_content_bytes
        truncated = len(text.encode()) > limit
        return ContentBlock(
            type="text",
            text=text.encode()[:limit].decode("utf-8", errors="ignore") if truncated else text,
            uri=uri,
            truncated=truncated,
        )

    def _oversized(self, value: str) -> bool:
        """Whether a base64 or text payload exceeds the block bound."""
        return len(value.encode()) > self._limits.max_content_bytes


def arguments_digest(arguments: Mapping[str, Any] | None) -> str:
    """A stable digest of a call's arguments.

    It binds an approval to what the owner was shown. JSON with sorted keys, so
    the same arguments always produce the same digest regardless of the order a
    client happened to serialise them in.
    """
    payload = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def encode_arguments(arguments: Mapping[str, Any] | None) -> str:
    """Serialize a call's arguments for protected storage."""
    return base64.b64encode(
        json.dumps(arguments or {}, separators=(",", ":"), default=str).encode()
    ).decode()


def decode_arguments(payload: str) -> dict[str, Any]:
    """Read back a protected argument body."""
    raw = base64.b64decode(payload.encode())
    document = json.loads(raw)
    return document if isinstance(document, dict) else {}


def new_invocation_id() -> str:
    """A fresh identifier for a held call."""
    return uuid.uuid4().hex


__all__ = [
    "MAX_CONTENT_BLOCKS",
    "MAX_CONTENT_BYTES",
    "ConnectorRequired",
    "ContentBlock",
    "HostError",
    "HostLimits",
    "McpHost",
    "Session",
    "SessionFingerprint",
    "SessionKey",
    "ToolResult",
    "arguments_digest",
    "decode_arguments",
    "encode_arguments",
    "new_invocation_id",
]
