"""The MCP relay: this service's own, and the only door to its tools.

MCP belongs here. This service owns the registry, each upstream's credential,
the job that requested a server and the attempt fence that says whether that
job is still running — every input a tool-call decision needs. The gateway had
the token and none of the state, which is why the ownership amendment moved
this whole surface across.

**Two credentials, deliberately not interchangeable.** A sandbox carries an
``agr`` grant for model calls, which the gateway mints and verifies, and an
``ajt`` token scoped ``mcp`` for tools, which this service mints and verifies.
A leaked grant buys no tools; a leaked MCP token buys no inference, no event
log, no patch and no terminal state.

**The scope check is positive.** ``parse_worker_token`` reads a token minted
before scopes existed as ``full`` — the conservative reading for signing, and
exactly the wrong one here: a check written as "not full" would admit every
such token to the tool surface. So this requires ``scope == SCOPE_MCP`` and
refuses everything else, ``full`` included. A runner has no business making
tool calls.

**The upstream credential never travels.** A server's URL and headers stay in
this process; the sandbox addresses the relay, and the relay addresses the
upstream. The response is filtered before it goes back, so a tool outside the
server's allowlist is neither advertised nor callable.

**The credential is the job owner's, and the owner is read from the fence.**
Servers resolve per user, so this has to name one — and the only defensible
choice is the person whose job this is, taken from the same fence lookup that
decides whether the token is still live. Not the caller: the caller is a
sandbox running untrusted repository content, and it does not get to say whose
tools it is spending.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from cloud_agent.api.deps import get_mcp_decision_signal, get_storage
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import protocol
from cloud_agent.mcp.authorization import decide, visible_capabilities
from cloud_agent.mcp.host import HostError, McpHost, SessionKey, arguments_digest
from cloud_agent.mcp.invocations import deadline_for
from cloud_agent.mcp.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST, PARSE_ERROR
from cloud_agent.mcp.models import CapabilityKind, McpServer
from cloud_agent.mcp.net_policy import policy_from_environment
from cloud_agent.mcp.refresh import RefreshRefused, ensure_fresh
from cloud_agent.mcp.registry import (
    binding_from_row,
    invocation_cipher,
    resolve_connection,
    server_for,
)
from cloud_agent.mcp.resources import (
    ResourceResolutionError,
    resource_capability,
    resource_uri,
    scope_resource_result,
    split_resource_uri,
)
from cloud_agent.mcp.transports import TransportError, guard_target, pinned_transport
from cloud_agent.mcp_proxy import (
    MAX_BUFFERED_RESPONSE_BYTES,
    METHOD_NOT_FOUND,
    SseFilter,
    filter_json_body,
    plan_request,
)
from cloud_agent.source_control import SourceControlError
from cloud_agent.tokens import SCOPE_MCP, InvalidAgentToken, parse_worker_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.ports import (
        CredentialQueries,
        McpConnectionRepository,
    )
    from cloud_agent.workers.mcp_decision_signal import McpDecisionSignal


logger = get_logger(__name__)


class _McpRelayStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    credential_queries: CredentialQueries
    mcp: McpConnectionRepository


router = APIRouter(prefix="/v1/agent/mcp")

# An MCP call is a tool call: it can be a repository search or an issue fetch,
# and it is not a token stream. Generous, but bounded -- a sandbox waiting on a
# hung upstream is a job burning its wall-clock timeout for nothing.
_DEFAULT_UPSTREAM_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 10.0

#: A held call waits for its owner's decision, which wakes it, and looks again
#: at least this often anyway: its caller leaving, its attempt being superseded
#: and its connection changing announce nothing.
_APPROVAL_IDLE_S = 5.0
#: How often it looks where nothing would wake it: no listener in this process,
#: or the listening connection lost.
_APPROVAL_POLL_S = 1.0

# Forwarded from the agent to the upstream. Everything else -- Authorization
# above all, but equally Cookie, X-Forwarded-*, and whatever else the CLI or an
# intermediary attaches -- is dropped: the upstream sees a request this control
# plane made, carrying the owner's credential and nothing of the sandbox's.
_FORWARD_TO_UPSTREAM = (
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
)

# Returned from the upstream to the agent. Session id must survive for
# streamable HTTP to work at all; the rest is this relay's to decide, and
# Set-Cookie in particular must never reach a sandbox.
_RETURN_TO_AGENT = ("content-type", "mcp-session-id", "mcp-protocol-version")

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


@dataclass
class Authorized:
    """The current fenced connection and any dispatch receipts for one request.

    Bundled rather than returned as a tuple because the fields are not
    interchangeable: a caller that swapped the binding for the connection would
    compile and authorize the wrong thing.
    """

    server: McpServer
    job_id: str
    user_id: str
    thread_id: str
    connection_id: int
    alias: str
    binding: Any
    connection: Any
    snapshot: Any
    #: The decrypted upstream credential, or ``None`` for a connection that
    #: authenticates by position or not at all. Carried because the merged host
    #: dials the upstream itself; the per-connection route builds its header set
    #: from the same value one field over and never reads this.
    secret: str | None = None
    dispatches: list[tuple[int, int]] = field(default_factory=list, compare=False)


class _BindingDisabled(HTTPException):
    """A session selection stops all connection work before credentials are read."""

    def __init__(self) -> None:
        """Refuse transport access for an explicitly disabled selection."""
        super().__init__(403, "this MCP connection is not enabled for this session")


def _error_reply(request_id: Any, message: str, code: int = METHOD_NOT_FOUND) -> dict[str, Any]:
    """Build a JSON-RPC error reply for one request id.

    Answered as a *protocol* error rather than an HTTP failure on purpose: the
    transport hop succeeded and the call did not, which is a thing models
    handle. A 403 here would look to the CLI like a broken connection and take
    the whole server down for the rest of the job.
    """
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result_reply(request_id: Any, result: Any, *, stateless: bool) -> dict[str, Any]:
    """Build a JSON-RPC result reply, discriminated when the client is modern.

    The stateless revision requires every result to carry ``resultType``. A
    client that asked in that shape treats a result without one as complete
    anyway, but sending it costs nothing and keeps the reply honest about which
    contract it was built for.
    """
    if stateless and isinstance(result, dict):
        result = {**result, "resultType": protocol.RESULT_TYPE_COMPLETE}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _bearer(authorization: str | None) -> str:
    """Extract the sandbox's MCP token, or refuse."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "invalid_request_error",
                    "message": "This endpoint takes an MCP-scoped job token.",
                }
            },
        )
    return token


def _refused(status_code: int, kind: str, message: str) -> HTTPException:
    """Build the JSON error shape every refusal on this surface uses."""
    return HTTPException(
        status_code=status_code, detail={"error": {"type": kind, "message": message}}
    )


async def _authorize_fence(
    authorization: str | None,
    store: _McpRelayStorage | None,
) -> dict[str, Any]:
    """Resolve the token to the job it was minted for, or refuse.

    Split from the per-alias resolution below because the host endpoint serves
    every binding of one job from a single URL: it needs the fence once and the
    bindings many times, and re-reading the fence per tool would make a
    revocation take effect at a different moment depending on which endpoint
    asked.

    """
    token = _bearer(authorization)

    try:
        claims = parse_worker_token(token)
    except InvalidAgentToken as exc:
        raise _refused(401, "invalid_request_error", str(exc)) from exc

    # **Positive, and it has to be.** `parse_worker_token` reads a token minted
    # before scopes existed as `full`, which is the conservative reading when
    # deciding whether a signature is trustworthy and precisely the wrong one
    # here -- a check written as "not full" would admit every such token. A
    # runner's full-scope token is refused too: reporting events and driving
    # terminal transitions is not a reason to be able to call somebody's tools.
    if claims.get("scope") != SCOPE_MCP:
        raise _refused(403, "insufficient_scope", "This endpoint requires an MCP-scoped token.")

    if store is None:
        raise _refused(
            503, "invalid_request_error", "MCP is unavailable without a configured database."
        )

    # The fence, read fresh on every call. The token names an attempt; this
    # asks whether that attempt is still the job's current one, still running,
    # with an unexpired lease. So revocation is automatic -- the instant the
    # reaper supersedes the attempt or the owner cancels, the same token stops
    # buying tools, with no key to remember to revoke.
    identity = await store.credential_queries.resolve_model_credential(
        job_id=claims["job_id"],
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if identity is None:
        raise _refused(
            401, "invalid_request_error", "This job token is no longer valid for tool calls."
        )
    return identity


async def _resolve_binding(
    identity: dict[str, Any],
    alias: str,
    store: _McpRelayStorage,
    *,
    refresh_credentials: bool = True,
) -> Authorized:
    """Resolve one binding alias into the server this job may reach."""
    job_id = identity["job_id"]
    matching = [item for item in identity.get("mcp_bindings") or [] if item.get("alias") == alias]
    if len(matching) > 1:
        raise _refused(
            403,
            "permission_error",
            f"More than one MCP connection uses alias {alias!r}. Save a unique selection in Tools.",
        )
    binding_row = matching[0] if matching else None
    if binding_row is None:
        # The session's own bindings, not a catalogue. A token is scoped to the
        # job it was minted for, and a job reaches exactly the connections its
        # conversation enabled.
        logger.warning(
            "agent_mcp_binding_not_granted",
            extra={
                "event": "agent_mcp_binding_not_granted",
                "job_id": job_id,
                "alias": alias,
            },
        )
        raise _refused(
            403,
            "permission_error",
            f"This session is not bound to an MCP connection named {alias!r}.",
        )

    if (
        binding_row.get("deleted_at") is not None
        or binding_row.get("owner_id") != identity["user_id"]
    ):
        raise _refused(404, "not_found", f"No MCP connection named {alias!r} is bound here.")

    if not binding_row.get("enabled", True):
        raise _BindingDisabled()

    connection_row = await store.mcp.get_mcp_connection(
        connection_id=int(binding_row["connection_id"])
    )
    if connection_row is None:
        raise _refused(503, "server_error", f"The {alias!r} MCP connection no longer exists.")
    if (
        not connection_row.get("enabled", True)
        or connection_row.get("status") in {"disabled", "error"}
        or connection_row.get("deleted_at") is not None
    ):
        raise _refused(503, "server_error", f"The {alias!r} MCP connection is not usable.")
    if str(connection_row.get("transport")) == "stdio":
        # A local process needs a worker or a connector to run it, and neither
        # is this listener. Refused here rather than forwarded to an address
        # that does not exist, so the agent is told what is missing instead of
        # reporting a server that cannot be reached.
        logger.warning(
            "agent_mcp_stdio_not_runnable",
            extra={"event": "agent_mcp_stdio_not_runnable", "job_id": job_id, "alias": alias},
        )
        # Named precisely, because the two cases need different actions from the
        # owner: one is a deployment that has no connector runtime, the other is
        # a connection they have not routed anywhere yet.
        raise _refused(
            503,
            "server_error",
            (
                f"The {alias!r} MCP connection runs through a connector, and this "
                "deployment does not serve connectors yet."
                if connection_row.get("connector_id")
                else f"The {alias!r} MCP connection runs a local process, and no connector "
                "is configured for it."
            ),
        )

    try:
        # Before the credential is read, so a token that aged out during a long
        # job is renewed instead of being sent and refused.
        if refresh_credentials:
            connection_row = dict(await ensure_fresh(store.mcp, row=connection_row))
    except RefreshRefused as exc:
        logger.warning(
            "agent_mcp_refresh_refused",
            extra={
                "event": "agent_mcp_refresh_refused",
                "job_id": job_id,
                "alias": alias,
                "kind": exc.kind,
            },
        )
        raise _refused(
            503,
            "server_error",
            f"The {alias!r} MCP connection needs to be reconnected.",
        ) from exc

    resolved = await resolve_connection(store.mcp, row=connection_row)
    if not resolved.usable:
        # Every method, not just ``tools/call``. ``decide`` covers the call path,
        # but ``initialize``, ``tools/list`` and the session verbs would
        # otherwise still be forwarded -- to a server this owner has turned off,
        # or with no credential at all when the deployment lost its encryption
        # key. A relay that dials a disconnected connection is a disconnect that
        # did not happen.
        logger.warning(
            "agent_mcp_connection_unusable",
            extra={
                "event": "agent_mcp_connection_unusable",
                "job_id": job_id,
                "alias": alias,
                "kind": resolved.error_kind.value,
            },
        )
        raise _refused(503, "server_error", f"The {alias!r} MCP connection is not usable.")
    binding = binding_from_row(binding_row)
    if binding.selection_pending and resolved.snapshot is not None:
        # A migrated session advertises what the server offers and confirms
        # every call, which is how it keeps working without inheriting an
        # approval its owner never gave.
        allowed = visible_capabilities(binding, resolved.snapshot.tools, CapabilityKind.TOOL)
    else:
        allowed = visible_capabilities(
            binding, resolved.snapshot.tools if resolved.snapshot else (), CapabilityKind.TOOL
        )
    server = server_for(resolved, binding, tools=allowed)
    return Authorized(
        server=server,
        job_id=job_id,
        user_id=identity["user_id"],
        thread_id=identity["thread_id"],
        connection_id=int(binding_row["connection_id"]),
        alias=alias,
        binding=binding,
        connection=resolved.connection,
        snapshot=resolved.snapshot,
        secret=resolved.credential.get("secret"),
    )


async def _authorize(
    alias: str,
    authorization: str | None,
    store: _McpRelayStorage | None,
) -> Authorized:
    """Resolve the token, the session's binding and the owner's connection."""
    identity = await _authorize_fence(authorization, store)
    if store is None:  # pragma: no cover - _authorize_fence refuses a missing store
        raise _refused(
            503, "invalid_request_error", "MCP is unavailable without a configured database."
        )
    return await _resolve_live_binding(
        identity,
        alias,
        store,
        attempt_id=_attempt_id_of(authorization),
        lease_generation=_lease_of(authorization),
    )


async def _resolve_live_binding(
    identity: dict[str, Any],
    alias: str,
    store: _McpRelayStorage,
    *,
    attempt_id: int | None,
    lease_generation: int,
) -> Authorized:
    """Refresh only the selected connection, then recheck its live authority.

    OAuth can spend seconds on the network. The binding and attempt read before
    that wait cannot authorize dispatch after the user changes or revokes them.
    The second resolution reads current rows without another network refresh.
    """
    previous = await _resolve_binding(identity, alias, store)
    current_identity = (
        await store.credential_queries.resolve_model_credential(
            job_id=previous.job_id, attempt_id=attempt_id, lease_generation=lease_generation
        )
        if attempt_id is not None
        else None
    )
    if current_identity is None:
        raise _refused(401, "permission_error", "This MCP session is no longer authorized.")
    current = await _resolve_binding(current_identity, alias, store, refresh_credentials=False)
    if (
        current.connection_id != previous.connection_id
        or current.binding != previous.binding
        or current.connection.config_revision != previous.connection.config_revision
        or current.connection.authorization_epoch != previous.connection.authorization_epoch
        or current.connection.capability_revision != previous.connection.capability_revision
    ):
        raise _refused(
            403, "permission_error", "MCP authorization changed while refreshing credentials."
        )
    return current


async def _refresh_catalogue_entry(
    entry: tuple[Authorized, Any], authorization: str | None, store: _McpRelayStorage
) -> tuple[Authorized, Any]:
    """Refresh the chosen tool or prompt and use its current capability record."""
    previous, capability = entry
    current = await _authorize(previous.alias, authorization, store)
    if current.connection_id != previous.connection_id:
        raise _refused(403, "permission_error", "The selected MCP connection changed.")
    latest = (
        next(
            (
                item
                for item in current.snapshot.of_kind(capability.kind)
                if item.name == capability.name
            ),
            None,
        )
        if current.snapshot is not None
        else None
    )
    if latest is None:
        raise _refused(403, "permission_error", "This MCP capability is no longer available.")
    return current, latest


def _binding_error(request_id: Any, exc: HTTPException) -> dict[str, Any]:
    """Return a selected-connection failure without taking down the merged endpoint."""
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        detail = detail["error"].get("message")
    return _error_reply(request_id, str(detail or "This MCP connection is unavailable."))


async def _decide_call(
    authorized: Authorized,
    store: _McpRelayStorage,
    *,
    name: str,
    arguments: Any,
    capability: Any,
    attempt_id: int | None,
    lease_generation: int,
    request_id: Any,
    kind: CapabilityKind = CapabilityKind.TOOL,
    request: Request | None = None,
) -> dict[str, Any] | None:
    """Decide one call, or return ``None`` when it may be dispatched.

    The single decision point on this surface, for all three of them. The
    per-connection route and the merged host route call it, and so do tools,
    resources and prompts -- because two copies of an authorization rule is one
    copy that will be forgotten, and the copy that gets forgotten is always the
    newer one. ``kind`` selects the scope: a resource read is checked against
    the resource rules, which is what stops it being a way to reach something
    the session did not enable.
    """
    decision = decide(
        connection=authorized.connection,
        binding=authorized.binding,
        capability=capability,
        kind=kind,
        name=name,
        user_id=authorized.user_id,
    )
    if decision.allowed:
        return None
    if decision.refused:
        logger.warning(
            "agent_mcp_tool_refused",
            extra={
                "event": "agent_mcp_tool_refused",
                "job_id": authorized.job_id,
                "alias": authorized.alias,
                "tool": name,
                "code": decision.code,
            },
        )
        return _error_reply(request_id, decision.message or "this tool is not enabled")

    digest = arguments_digest(arguments if isinstance(arguments, dict) else None)
    # What the approval would have to have been given for. Every one of these
    # moves when something the owner agreed to changes: the target, the
    # catalogue, the session's own rules, or whose job this is.
    fence = {
        "config_revision": authorized.connection.config_revision,
        "authorization_epoch": authorized.connection.authorization_epoch,
        "capability_revision": authorized.connection.capability_revision,
        "policy_revision": authorized.binding.policy_revision,
        "attempt_id": attempt_id,
        "lease_generation": lease_generation or None,
    }
    approved = await store.mcp.find_approved_mcp_invocation(
        job_id=authorized.job_id,
        connection_id=authorized.connection_id,
        capability_name=name,
        arguments_digest=digest,
        capability_kind=kind.value,
        **fence,
    )
    if approved is not None:
        claimed = await store.mcp.claim_mcp_invocation(invocation_id=int(approved["id"]), **fence)
        if claimed is not None:
            # Exactly one dispatcher ever wins this UPDATE, which is why a
            # retry storm cannot send a write twice.
            authorized.dispatches.append((int(claimed["id"]), int(claimed["dispatch_generation"])))
            return None
    invocation = await store.mcp.create_mcp_invocation(
        job_id=authorized.job_id,
        attempt_id=attempt_id,
        lease_generation=lease_generation,
        thread_id=authorized.thread_id,
        connection_id=authorized.connection_id,
        capability_name=name,
        capability_kind=kind.value,
        arguments_digest=digest,
        arguments_ciphertext=_protect_arguments(arguments),
        config_revision=authorized.connection.config_revision,
        authorization_epoch=authorized.connection.authorization_epoch,
        capability_revision=authorized.connection.capability_revision,
        policy_revision=authorized.binding.policy_revision,
        expires_at=deadline_for(created_at=datetime.now(timezone.utc)).expires_at,
    )
    logger.info(
        "agent_mcp_call_held",
        extra={
            "event": "agent_mcp_call_held",
            "job_id": authorized.job_id,
            "alias": authorized.alias,
            "tool": name,
            "invocation_id": invocation["id"],
        },
    )
    return await _wait_for_approval(
        authorized,
        store,
        invocation=invocation,
        fence=fence,
        request_id=request_id,
        request=request,
    )


async def _wait_for_approval(
    authorized: Authorized,
    store: _McpRelayStorage,
    *,
    invocation: dict[str, Any],
    fence: dict[str, Any],
    request_id: Any,
    request: Request | None,
) -> dict[str, Any] | None:
    """Keep the original CLI call open and dispatch it once its owner approves.

    Waiting owns no transaction. The normal attempt heartbeat continues, and
    each look checks whether the attempt or selected connection changed. A
    disconnected caller leaves no reusable approval behind. The owner's
    decision wakes the wait; otherwise it looks again on a clock.
    """
    invocation_id = int(invocation["id"])
    signal = get_mcp_decision_signal(request) if request is not None else None
    subscription = (
        signal.subscribe(str(invocation_id)) if signal is not None else contextlib.nullcontext()
    )
    with subscription as woken:
        try:
            while True:
                if request is not None and await request.is_disconnected():
                    raise asyncio.CancelledError
                if datetime.now(timezone.utc) >= invocation["expires_at"]:
                    break
                attempt_id = fence["attempt_id"]
                if attempt_id is None:
                    break
                identity = await store.credential_queries.resolve_model_credential(
                    job_id=authorized.job_id,
                    attempt_id=attempt_id,
                    lease_generation=int(fence["lease_generation"] or 0),
                )
                if identity is None:
                    break
                try:
                    current = await _resolve_live_binding(
                        identity,
                        authorized.alias,
                        store,
                        attempt_id=attempt_id,
                        lease_generation=int(fence["lease_generation"] or 0),
                    )
                except HTTPException:
                    break
                if (
                    not current.binding.enabled
                    or current.connection_id != authorized.connection_id
                    or current.connection.config_revision != fence["config_revision"]
                    or current.connection.authorization_epoch != fence["authorization_epoch"]
                    or current.connection.capability_revision != fence["capability_revision"]
                    or current.binding.policy_revision != fence["policy_revision"]
                ):
                    break
                row = await store.mcp.get_mcp_invocation(invocation_id=invocation_id)
                if row is None:
                    break
                if row["status"] == "approved":
                    claimed = await store.mcp.claim_mcp_invocation(
                        invocation_id=invocation_id, **fence
                    )
                    if claimed is not None:
                        # OAuth may have refreshed while the user was deciding.
                        # Use the checked connection's current credential for dispatch.
                        authorized.server = current.server
                        authorized.connection = current.connection
                        authorized.secret = current.secret
                        authorized.dispatches.append(
                            (invocation_id, int(claimed["dispatch_generation"]))
                        )
                        return None
                    break
                if row["status"] != "pending":
                    return _error_reply(
                        request_id, f"MCP call {row['status']}; it was not dispatched."
                    )
                await _until_decided(signal, woken, expires_at=invocation["expires_at"])
        except asyncio.CancelledError:
            await asyncio.shield(store.mcp.expire_mcp_invocation(invocation_id=invocation_id))
            raise
    await store.mcp.expire_mcp_invocation(invocation_id=invocation_id)
    return _error_reply(request_id, "MCP approval expired or the session authorization changed.")


async def _until_decided(
    signal: McpDecisionSignal | None, woken: asyncio.Event | None, *, expires_at: datetime
) -> None:
    """Wait for the held call's decision, its expiry, or the next look anyway."""
    look = _APPROVAL_IDLE_S if signal is not None and signal.listening else _APPROVAL_POLL_S
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    timeout_s = max(0.0, min(look, remaining))
    if signal is None or woken is None:
        await asyncio.sleep(timeout_s)
    else:
        await signal.wait(woken, timeout_s)


async def _finish_dispatches(
    authorized: Authorized, store: _McpRelayStorage, *, status: str
) -> None:
    """Record an observed outcome without retrying a possible external write."""
    for invocation_id, generation in authorized.dispatches:
        await store.mcp.finish_mcp_invocation(
            invocation_id=invocation_id,
            dispatch_generation=generation,
            status=status,
        )
    authorized.dispatches.clear()


#: Methods that reach a capability, and what kind it is. The value is the
#: params field that names it: a tool and a prompt by name, a resource by URI,
#: because that is what the server itself uses as the identifier.
_CAPABILITY_METHODS: dict[str, tuple[CapabilityKind, str]] = {
    protocol.TOOLS_CALL: (CapabilityKind.TOOL, "name"),
    protocol.RESOURCES_READ: (CapabilityKind.RESOURCE, "uri"),
    protocol.RESOURCES_SUBSCRIBE: (CapabilityKind.RESOURCE, "uri"),
    protocol.RESOURCES_UNSUBSCRIBE: (CapabilityKind.RESOURCE, "uri"),
    protocol.PROMPTS_GET: (CapabilityKind.PROMPT, "name"),
}

#: Methods that reach no capability: housekeeping, and the catalogue reads whose
#: *responses* are filtered down to the reachable set on the way back.
_UNDECIDED_METHODS = frozenset(
    {
        protocol.INITIALIZE,
        protocol.INITIALIZED,
        protocol.PING,
        protocol.TOOLS_LIST,
        protocol.RESOURCES_LIST,
        protocol.RESOURCES_TEMPLATES_LIST,
        protocol.PROMPTS_LIST,
    }
)


async def _capability_decision(
    authorized: Authorized,
    store: _McpRelayStorage,
    body: bytes,
    *,
    attempt_id: int | None,
    lease_generation: int,
    request: Request | None = None,
) -> Any | None:
    """Decide one relayed body, or ``None`` to forward it.

    Every method reaches this, not only ``tools/call``. The per-connection route
    is a direct line to a user's server carrying that user's credential, so a
    method that skips the decision is a way to reach something the session
    turned off -- which is exactly what a proxy that authorized tools and
    forwarded everything else did with ``resources/read`` and ``prompts/get``.

    A method this deployment has no authorization story for is *refused*, not
    forwarded. Forwarding it would be the same mistake in the other direction:
    the platform would be proxying a capability it cannot reason about, with a
    credential it is responsible for. Notifications are the exception, because
    they ask for nothing and carry no reply.
    """
    try:
        document = json.loads(body or b"{}")
    except (json.JSONDecodeError, ValueError):
        return None
    payloads = document if isinstance(document, list) else [document]
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        method = str(payload.get("method") or "")
        if not method or method in _UNDECIDED_METHODS or method.startswith("notifications/"):
            continue
        entry = _CAPABILITY_METHODS.get(method)
        if entry is None:
            return _error_reply(
                payload.get("id"),
                f"This deployment does not proxy {method!r}. It carries the calls it "
                "can authorize; anything else is refused rather than forwarded.",
            )
        kind, key = entry
        params = payload.get("params")
        name = str(params.get(key) or "") if isinstance(params, dict) else ""
        capability = next(
            (
                item
                for item in (authorized.snapshot.of_kind(kind) if authorized.snapshot else ())
                if item.name == name
            ),
            None,
        )
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if kind is CapabilityKind.RESOURCE:
            if not isinstance(params, dict) or not isinstance(params.get(key), str) or not name:
                return _error_reply(payload.get("id"), f"{method} needs a uri", INVALID_PARAMS)
            try:
                capability = resource_capability(authorized.snapshot, authorized.binding, name)
            except ResourceResolutionError as exc:
                return _error_reply(payload.get("id"), str(exc))
            arguments = {"uri": name, "method": method}
            if capability is not None:
                kind, name = capability.kind, capability.name
        held = await _decide_call(
            authorized,
            store,
            name=name,
            arguments=arguments,
            capability=capability,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            request_id=payload.get("id"),
            kind=kind,
            request=request,
        )
        if held is not None:
            return held
    return None


def _protect_arguments(arguments: Any) -> str | None:
    """Store a held call's arguments under the invocation cipher."""
    if not isinstance(arguments, dict):
        return None
    from cloud_agent.mcp.host import encode_arguments

    try:
        return invocation_cipher().encrypt(encode_arguments(arguments))
    except SourceControlError:
        # Without a key the platform cannot hold the body. The call is still
        # held -- the owner can approve it by name -- but the arguments will not
        # be shown, which is worse than useless for a write, so this is logged.
        logger.error("agent_mcp_invocation_arguments_unprotectable")
        return None


def _upstream_headers(request: Request, server: McpServer) -> dict[str, str]:
    """Build the outbound header set: the agent's protocol, our credential.

    Merged case-insensitively, which is not a nicety. HTTP header names are
    case-insensitive but ``dict`` keys are not, and the forwarded set arrives
    lowercased from Starlette while a registry writes ``Authorization`` the way
    a human does. A naive ``update`` therefore keeps *both*, httpx sends both,
    and the sandbox's own token rides along beside the credential meant to
    replace it.
    """
    headers = {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() in _FORWARD_TO_UPSTREAM
    }
    # The registry always wins over anything the sandbox sent under the same
    # name — now that the same name is actually recognized as the same name.
    headers.update({key.lower(): value for key, value in server.headers.items()})
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    """Pick what may travel back to the sandbox."""
    return {
        key: value for key, value in upstream.headers.items() if key.lower() in _RETURN_TO_AGENT
    }


def build_upstream_client(request_timeout_s: float | None = None) -> httpx.AsyncClient:
    """Build the client the proxy dials upstream with.

    Redirects are not followed: a redirect would re-send this deployment's
    credential to whatever host the response named.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            request_timeout_s or _DEFAULT_UPSTREAM_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S
        ),
        follow_redirects=False,
        transport=pinned_transport(),
    )


async def _proxy(
    request: Request,
    *,
    server: McpServer,
    job_id: str,
    method: str,
    body: bytes = b"",
) -> Any:
    """Forward one request upstream and stream or filter the reply back.

    The target is validated here, at the dial, and not only when the connection
    was saved. A URL that was acceptable when it was typed may resolve somewhere
    else now, and this route is the one that actually opens the socket -- a
    check that lives only in the save path is a check the call path does not
    have.
    """
    try:
        await guard_target(server.url or "", policy_from_environment())
    except TransportError as exc:
        logger.warning(
            "agent_mcp_relay_target_refused",
            extra={
                "event": "agent_mcp_relay_target_refused",
                "job_id": job_id,
                "connection_id": server.connection_id,
                "detail": exc.kind,
            },
        )
        raise _refused(
            503,
            "server_error",
            f"The {server.name!r} MCP connection could not be reached: {exc}",
        ) from exc
    assert server.url is not None
    client = build_upstream_client(server.request_timeout_s)
    upstream_request = client.build_request(
        method,
        server.url,
        content=body or None,
        headers=_upstream_headers(request, server),
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning(
            "agent_mcp_upstream_error",
            extra={"event": "agent_mcp_upstream_error", "job_id": job_id, "server": server.name},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "server_error",
                    "message": f"The {server.name!r} MCP server could not be reached.",
                }
            },
        ) from exc

    if upstream.status_code in (401, 403):
        # The third way a connection dies, and the only one visible from here.
        # Expiry and an unreadable credential are caught during resolution; an
        # upstream that has simply stopped accepting a live-looking token can
        # only be discovered by asking it. Attributed to the server, because
        # unattributed it reads as the sandbox misbehaving when the truth is
        # that this owner's connection needs re-authorizing — the shape the
        # gateway's route learned after an hour of silent 401s
        # (hybridInference#1199) and which came across with this surface.
        #
        # Logged, not converted: the status belongs to the agent's own request
        # and rewriting it would hide a real answer from the CLI. Feeding this
        # back into the connection's status, so Integrations says "connect it
        # again" before the next job fails, needs a place to record it and
        # belongs with the connect flow that writes the row.
        logger.warning(
            "agent_mcp_upstream_rejected_credential",
            extra={
                "event": "agent_mcp_upstream_rejected_credential",
                "job_id": job_id,
                "server": server.name,
                "status": upstream.status_code,
            },
        )

    content_type = upstream.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        return StreamingResponse(
            _stream_sse(upstream, client, server),
            status_code=upstream.status_code,
            headers={**_response_headers(upstream), **_SSE_HEADERS},
            media_type=content_type,
        )

    try:
        chunks: list[bytes] = []
        total = 0
        async for chunk in upstream.aiter_bytes():
            total += len(chunk)
            if total > MAX_BUFFERED_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {
                            "type": "server_error",
                            "message": (
                                f"The {server.name!r} MCP server returned more than "
                                "this gateway will buffer for one reply."
                            ),
                        }
                    },
                )
            chunks.append(chunk)
    except httpx.HTTPError as exc:
        # A server that stops answering mid-body is the same event as one that
        # never answered, and it has to leave as the same 502: an unconverted
        # transport error becomes a 500 whose message says nothing, and the
        # caller cannot tell it from this service breaking.
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "server_error",
                    "message": f"The {server.name!r} MCP server stopped answering.",
                }
            },
        ) from exc
    finally:
        await upstream.aclose()
        await client.aclose()

    payload = b"".join(chunks)
    if content_type.startswith("application/json"):
        payload = filter_json_body(payload, server)
    return Response(
        content=payload,
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
    )


async def _stream_sse(
    upstream: httpx.Response, client: httpx.AsyncClient, server: McpServer
) -> AsyncIterator[bytes]:
    """Relay an SSE reply, rewriting tool catalogues as they pass."""
    sse = SseFilter(server)
    try:
        async for chunk in upstream.aiter_bytes():
            if out := sse.feed(chunk):
                yield out
        if tail := sse.flush():
            yield tail
    finally:
        await upstream.aclose()
        await client.aclose()


# --------------------------------------------------------------------------
# The merged host endpoint
# --------------------------------------------------------------------------
#
# One URL that presents every connection this job is bound to as a single MCP
# server, with tool names namespaced by binding alias.
#
# The per-connection route above exists because a CLI can be pointed at one
# server at a time. That is enough for the three CLIs whose MCP client the
# platform configures directly, and not enough for a runtime whose MCP entries
# are gated by a name-and-identity allowlist it cannot extend at run time: there
# the *name* has to be knowable before the job starts, and a per-conversation
# alias never is. One fixed name and one fixed URL is the shape that fits.
#
# It is also the design's "relay as protocol host" step. The per-connection
# route forwards JSON-RPC and lets the CLI own the protocol; here the platform
# owns it -- it answers ``initialize``, merges the catalogues, routes a call by
# its namespaced name and normalizes what comes back. Which is why the
# authorization path is *shared* rather than reimplemented: the merged route is
# a second door onto the same decision function, never a second decision.


def _host() -> McpHost:
    """The process-wide protocol host, built once against the deployment policy."""
    global _HOST_INSTANCE
    if _HOST_INSTANCE is None:
        _HOST_INSTANCE = McpHost(policy=policy_from_environment())
    return _HOST_INSTANCE


_HOST_INSTANCE: McpHost | None = None
_HOST_MAINTENANCE_INTERVAL_S = 30.0


async def reap_host_sessions(store: _McpRelayStorage) -> None:
    """Reclaim sockets whose job or selected connection is no longer live."""

    async def is_live(key: SessionKey) -> bool:
        job_id, attempt_id = key.job_attempt
        identity = await store.credential_queries.resolve_model_credential(
            job_id=job_id,
            attempt_id=int(attempt_id),
            lease_generation=key.owner_lease_generation,
        )
        if identity is None:
            return False
        binding = next(
            (
                row
                for row in identity.get("mcp_bindings") or []
                if str(row["connection_id"]) == key.connection_id
            ),
            None,
        )
        if (
            binding is None
            or not binding.get("enabled", True)
            or binding.get("deleted_at") is not None
            or binding.get("owner_id") != identity["user_id"]
        ):
            return False
        connection = await store.mcp.get_mcp_connection(connection_id=int(key.connection_id))
        return (
            connection is not None
            and connection.get("status") != "disabled"
            and connection.get("deleted_at") is None
        )

    if _HOST_INSTANCE is not None:
        await _HOST_INSTANCE.reap_inactive(is_live)


async def maintain_host_sessions(store: _McpRelayStorage) -> None:
    """Run process-local socket cleanup and expire overdue approval rows."""
    while True:
        await asyncio.sleep(_HOST_MAINTENANCE_INTERVAL_S)
        try:
            async with asyncio.timeout(5.0):
                await store.mcp.expire_mcp_invocations()
        except Exception:
            logger.warning("agent_mcp_invocation_expiry_failed", exc_info=True)
        await reap_host_sessions(store)


async def close_host() -> None:
    """Release this listener's protocol host before its database pool closes."""
    global _HOST_INSTANCE
    instance, _HOST_INSTANCE = _HOST_INSTANCE, None
    if instance is not None:
        await instance.aclose()


async def _host_catalogue(
    identity: dict[str, Any],
    store: _McpRelayStorage,
    kind: CapabilityKind = CapabilityKind.TOOL,
) -> dict[str, tuple[Authorized, Any]]:
    """Every capability of one kind this job may use, keyed by what it is called.

    One builder for all three surfaces, because they differ only in what the
    client uses as the key: a tool and a prompt by name, a resource by URI.
    Building them separately is how a surface ends up with its own idea of which
    bindings are reachable, and the design is explicit that resources and
    prompts get their own *scopes* -- not their own resolution rules.

    A binding that cannot be resolved is skipped rather than fatal. One
    disconnected server must not take the others out of the catalogue -- the
    agent would lose every tool it does have, which is a far worse failure than
    the one that caused it. The per-connection route still refuses loudly for
    the server that is actually broken.

    Routing is a lookup in this map, not a string split. An alias may itself
    contain the separator, so splitting would send ``a__b__tool`` to the wrong
    server; the map is built from the catalogue, so a name that is not in it is
    genuinely not callable.
    """
    catalogue: dict[str, tuple[Authorized, Any]] = {}
    for binding_row in identity.get("mcp_bindings") or []:
        alias = str(binding_row.get("alias") or "")
        if not alias:
            continue
        try:
            # Discovery snapshots are local. Listing or routing one connection
            # must never contact another connection's OAuth endpoint.
            authorized = await _resolve_binding(identity, alias, store, refresh_credentials=False)
        except HTTPException as exc:
            logger.warning(
                "agent_mcp_host_binding_unavailable",
                extra={
                    "event": "agent_mcp_host_binding_unavailable",
                    "job_id": identity["job_id"],
                    "alias": alias,
                    "detail": str(exc.detail),
                },
            )
            continue
        available = authorized.snapshot.of_kind(kind) if authorized.snapshot else ()
        for capability in visible_capabilities(authorized.binding, available, kind):
            # Connection-qualified URIs keep resource identity stable when
            # other connections advertise the same upstream URI or template.
            key = (
                resource_uri(authorized.connection_id, capability.name)
                if kind in (CapabilityKind.RESOURCE, CapabilityKind.RESOURCE_TEMPLATE)
                else protocol.presented_name(alias, capability.name)
            )
            catalogue.setdefault(key, (authorized, capability))
    return catalogue


class _Catalogued(Protocol):
    """What a listing needs of the resolved binding it renders a tool from.

    Narrowed to a protocol so the renderer cannot reach for more of a
    connection than a name and an id, and so a test can hand it a stand-in.
    """

    @property
    def alias(self) -> str: ...
    @property
    def connection_id(self) -> int: ...


def _resource_entry(capability: Any, uri: str) -> dict[str, Any]:
    """Render one resource the way a client reads it."""
    schema = capability.input_schema if isinstance(capability.input_schema, dict) else {}
    uri_field = "uriTemplate" if capability.kind is CapabilityKind.RESOURCE_TEMPLATE else "uri"
    entry: dict[str, Any] = {uri_field: uri, "name": capability.name}
    if capability.description:
        entry["description"] = capability.description
    if schema.get("mimeType"):
        entry["mimeType"] = schema["mimeType"]
    return entry


def _prompt_entry(presented: str, capability: Any) -> dict[str, Any]:
    """Render one prompt, under the name this session will call it by."""
    schema = capability.input_schema if isinstance(capability.input_schema, dict) else {}
    entry: dict[str, Any] = {"name": presented}
    if capability.description:
        entry["description"] = capability.description
    if isinstance(schema.get("arguments"), list):
        entry["arguments"] = schema["arguments"]
    return entry


async def _host_session(authorized: Authorized, authorization: str | None) -> Any:
    """Open or reuse the host session for one connection, fenced to this attempt.

    The member identity is the job and attempt the token names, and the owner
    lease generation is part of the session key: a takeover produces a new key
    and leaves the superseded session to be reaped, rather than handing the new
    owner the old one's upstream state.
    """
    key = SessionKey(
        connection_id=str(authorized.connection_id),
        member=f"{authorized.job_id}:{_attempt_id_of(authorization) or 0}",
        owner_lease_generation=_lease_of(authorization),
    )
    return await _host().open(authorized.connection, secret=authorized.secret, key=key)


def _host_tool_entry(presented: str, authorized: _Catalogued, capability: Any) -> dict[str, Any]:
    """Render one merged catalogue entry the way a client reads it."""
    return {
        "name": presented,
        "description": capability.description
        or f"{capability.name} on the {authorized.alias} MCP server",
        "inputSchema": capability.input_schema
        if isinstance(capability.input_schema, dict)
        else {"type": "object", "properties": {}},
    }


async def _host_initialize(payload: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """Answer the handshake for the merged surface.

    The requested revision is echoed when this platform implements it, because
    the client decides in the handshake era and a server that answers with
    something else is asking the client to disconnect. When it is not one we
    implement, the newest we do is offered instead: a client that understands
    the list will pick from it, and one that does not is at least told what it
    is talking to rather than being driven with a guessed header.

    **Every surface this endpoint answers is advertised.** The three families
    are declared together because the client's next request comes from here: a
    handshake that named only ``tools`` would leave ``resources/list`` and
    ``prompts/list`` never asked for, and a capability the platform serves but
    does not claim is indistinguishable from one it does not have. The two
    sub-flags are the honest part of the claim -- ``listChanged`` is false
    because this surface is request/response and cannot push, and
    ``subscribe`` is false because it holds no resource subscription open
    between calls.
    """
    params = payload.get("params")
    requested = str(params.get("protocolVersion") or "") if isinstance(params, dict) else ""
    agreed = requested if requested in protocol.PROFILES else protocol.PREFERRED_PROTOCOL_VERSION
    aliases = sorted(str(row.get("alias") or "") for row in identity.get("mcp_bindings") or [])
    return {
        "protocolVersion": agreed,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "serverInfo": {"name": "hybridinference-cloud-agent", "version": "1"},
        "instructions": (
            "Use the exact tool names returned by tools/list. This session's connections: "
            + (", ".join(alias for alias in aliases if alias) or "none")
        ),
    }


async def _host_post(
    *,
    identity: dict[str, Any],
    store: _McpRelayStorage,
    request: Request,
    authorization: str | None,
) -> Any:
    """Dispatch one JSON-RPC message addressed to the merged surface."""
    body = await request.body()
    try:
        document = json.loads(body or b"{}")
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse(
            _error_reply(None, f"invalid JSON: {exc}", PARSE_ERROR), status_code=400
        )

    payloads = document if isinstance(document, list) else [document]
    replies: list[dict[str, Any]] = []
    stateless = False
    for payload in payloads:
        if not isinstance(payload, dict):
            replies.append(_error_reply(None, "every message must be an object", INVALID_REQUEST))
            continue
        params = payload.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        stateless = stateless or isinstance(meta, dict)
        method = str(payload.get("method") or "")
        if "id" not in payload:
            # A notification. ``notifications/initialized`` is the only one this
            # surface expects, and it is answered with an empty 202.
            continue
        request_id = payload.get("id")
        if method == protocol.INITIALIZE:
            replies.append(
                _result_reply(
                    request_id,
                    await _host_initialize(payload, identity),
                    stateless=False,
                )
            )
            continue
        if method == protocol.PING:
            replies.append(_result_reply(request_id, {}, stateless=stateless))
            continue
        if method == protocol.TOOLS_LIST:
            catalogue = await _host_catalogue(identity, store)
            replies.append(
                _result_reply(
                    request_id,
                    {
                        "tools": [
                            _host_tool_entry(name, authorized, capability)
                            for name, (authorized, capability) in sorted(
                                catalogue.items(), key=lambda item: item[0]
                            )
                        ]
                    },
                    stateless=stateless,
                )
            )
            continue
        if method == protocol.TOOLS_CALL:
            replies.append(
                await _host_call(
                    payload,
                    request_id=request_id,
                    identity=identity,
                    store=store,
                    authorization=authorization,
                    stateless=stateless,
                    request=request,
                )
            )
            continue
        if method in (
            protocol.RESOURCES_LIST,
            protocol.RESOURCES_TEMPLATES_LIST,
            protocol.PROMPTS_LIST,
        ):
            replies.append(
                await _host_listing(
                    method,
                    request_id=request_id,
                    identity=identity,
                    store=store,
                    stateless=stateless,
                )
            )
            continue
        if method in (protocol.RESOURCES_READ, protocol.PROMPTS_GET):
            replies.append(
                await _host_fetch(
                    payload,
                    method=method,
                    request_id=request_id,
                    identity=identity,
                    store=store,
                    authorization=authorization,
                    stateless=stateless,
                    request=request,
                )
            )
            continue
        replies.append(_error_reply(request_id, f"this surface does not implement {method!r}"))

    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(document, list) else replies[0])


async def _host_listing(
    method: str,
    *,
    request_id: Any,
    identity: dict[str, Any],
    store: _McpRelayStorage,
    stateless: bool,
) -> dict[str, Any]:
    """Answer one of the catalogue reads, filtered by this session's scopes.

    Only what the binding enabled is listed. That is the rule the tool catalogue
    already follows, and for the same reason: a model cannot be talked into
    reading a resource it was never offered, and a server with a hundred
    resources does not spend a small model's context on the ninety-nine this
    session may not touch.
    """
    if method == protocol.RESOURCES_LIST:
        catalogue = await _host_catalogue(identity, store, CapabilityKind.RESOURCE)
        return _result_reply(
            request_id,
            {
                "resources": [
                    _resource_entry(capability, uri) for uri, (_, capability) in catalogue.items()
                ]
            },
            stateless=stateless,
        )
    if method == protocol.RESOURCES_TEMPLATES_LIST:
        catalogue = await _host_catalogue(identity, store, CapabilityKind.RESOURCE_TEMPLATE)
        return _result_reply(
            request_id,
            {
                "resourceTemplates": [
                    _resource_entry(capability, uri) for uri, (_, capability) in catalogue.items()
                ]
            },
            stateless=stateless,
        )
    catalogue = await _host_catalogue(identity, store, CapabilityKind.PROMPT)
    return _result_reply(
        request_id,
        {
            "prompts": [
                _prompt_entry(presented, capability)
                for presented, (_, capability) in sorted(catalogue.items())
            ]
        },
        stateless=stateless,
    )


async def _host_fetch(
    payload: dict[str, Any],
    *,
    method: str,
    request_id: Any,
    identity: dict[str, Any],
    store: _McpRelayStorage,
    authorization: str | None,
    stateless: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    """Read one resource or fetch one prompt, under its own scope.

    Resolved through the same catalogue and decided by the same function as a
    tool call, which is the whole point: the design gives resources and prompts
    their own scopes so that they are not a way round tool authorization, and a
    separate code path is exactly how they would become one.
    """
    params = payload.get("params")
    settings = params if isinstance(params, dict) else {}
    reading = method == protocol.RESOURCES_READ
    key = settings.get("uri" if reading else "name")
    if not isinstance(key, str) or not key:
        return _error_reply(
            request_id, f"{method} needs a {'uri' if reading else 'name'}", INVALID_PARAMS
        )

    original_uri = ""
    if reading:
        try:
            connection_id, original_uri = split_resource_uri(key)
            binding = next(
                (
                    row
                    for row in identity.get("mcp_bindings") or []
                    if str(row.get("connection_id")) == connection_id
                ),
                None,
            )
            if binding is None:
                raise ResourceResolutionError(
                    "That resource connection is not bound to this session."
                )
            authorized = await _authorize(str(binding.get("alias") or ""), authorization, store)
            if str(authorized.connection_id) != connection_id:
                raise ResourceResolutionError("The selected resource connection changed.")
            capability = resource_capability(authorized.snapshot, authorized.binding, original_uri)
        except ResourceResolutionError as exc:
            return _error_reply(request_id, str(exc))
        except HTTPException as exc:
            return _binding_error(request_id, exc)
        if capability is None:
            return _error_reply(
                request_id, "That URI is not in this session's resource catalogue or templates."
            )
        kind = capability.kind
    else:
        kind = CapabilityKind.PROMPT
        catalogue = await _host_catalogue(identity, store, kind)
        entry = catalogue.get(key)
        if entry is None:
            return _error_reply(
                request_id,
                f"{key!r} is not one of this session's prompts. Call prompts/list for the current set.",
            )
        try:
            # Resources and prompts are connections only: the platform's own
            # tools are a tool surface, so this catalogue holds nothing that is
            # not a resolved connection.
            authorized, capability = await _refresh_catalogue_entry(
                cast("tuple[Authorized, Any]", entry), authorization, store
            )
        except HTTPException as exc:
            return _binding_error(request_id, exc)
    held = await _decide_call(
        authorized,
        store,
        name=capability.name,
        arguments=settings.get("arguments")
        if not reading
        else {"uri": original_uri, "method": method},
        capability=capability,
        attempt_id=_attempt_id_of(authorization),
        lease_generation=_lease_of(authorization),
        request_id=request_id,
        request=request,
        kind=kind,
    )
    if held is not None:
        return held

    try:
        session = await _host_session(authorized, authorization)
        if reading:
            result = await _host().read_resource(session, uri=original_uri)
        else:
            arguments = settings.get("arguments")
            result = await _host().get_prompt(
                session,
                name=capability.name,
                arguments=arguments if isinstance(arguments, dict) else None,
            )
    except asyncio.CancelledError:
        await asyncio.shield(_finish_dispatches(authorized, store, status="outcome_unknown"))
        raise
    except HostError as exc:
        await _finish_dispatches(authorized, store, status="outcome_unknown")
        logger.warning(
            "agent_mcp_host_read_failed",
            extra={
                "event": "agent_mcp_host_read_failed",
                "job_id": authorized.job_id,
                "alias": authorized.alias,
                "kind": kind.value,
                "method": method,
                "detail": exc.kind,
            },
        )
        return _error_reply(request_id, str(exc), INTERNAL_ERROR)
    await _finish_dispatches(authorized, store, status="completed")
    return _result_reply(
        request_id, scope_resource_result(result, authorized.connection_id), stateless=stateless
    )


async def _host_call(
    payload: dict[str, Any],
    *,
    request_id: Any,
    identity: dict[str, Any],
    store: _McpRelayStorage,
    authorization: str | None,
    stateless: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    """Authorize and dispatch one namespaced tool call."""
    params = payload.get("params")
    name = str(params.get("name") or "") if isinstance(params, dict) else ""
    arguments = params.get("arguments") if isinstance(params, dict) else None
    catalogue = await _host_catalogue(identity, store)
    entry = catalogue.get(name)
    if entry is None:
        return _error_reply(
            request_id,
            f"{name!r} is not one of this session's tools. Call tools/list for the current set.",
        )
    try:
        authorized, capability = await _refresh_catalogue_entry(
            cast("tuple[Authorized, Any]", entry), authorization, store
        )
    except HTTPException as exc:
        return _binding_error(request_id, exc)
    held = await _decide_call(
        authorized,
        store,
        name=capability.name,
        arguments=arguments,
        capability=capability,
        attempt_id=_attempt_id_of(authorization),
        lease_generation=_lease_of(authorization),
        request_id=request_id,
        request=request,
    )
    if held is not None:
        return held

    # The member identity is the job and attempt the token names, and the owner
    # lease generation is part of the session key: a takeover produces a new key
    # and leaves the superseded session to be reaped, rather than handing the
    # new owner the old one's upstream state.
    try:
        session = await _host_session(authorized, authorization)
        outcome = await _host().call_tool(
            session,
            name=capability.name,
            arguments=arguments if isinstance(arguments, dict) else {},
        )
    except asyncio.CancelledError:
        await asyncio.shield(_finish_dispatches(authorized, store, status="outcome_unknown"))
        raise
    except HostError as exc:
        await _finish_dispatches(authorized, store, status="outcome_unknown")
        logger.warning(
            "agent_mcp_host_call_failed",
            extra={
                "event": "agent_mcp_host_call_failed",
                "job_id": authorized.job_id,
                "alias": authorized.alias,
                "tool": capability.name,
                "kind": exc.kind,
            },
        )
        return _error_reply(request_id, str(exc), INTERNAL_ERROR)
    await _finish_dispatches(
        authorized, store, status="failed" if outcome.is_error else "completed"
    )
    return _result_reply(
        request_id,
        scope_resource_result(outcome.as_dict(), authorized.connection_id),
        stateless=stateless,
    )


@router.post("")
async def mcp_host_post(
    request: Request,
    authorization: str | None = Header(None),
    store: _McpRelayStorage | None = Depends(get_storage),
) -> Any:
    """Serve every connection of one job from a single MCP endpoint.

    Registered on the bare prefix, which is why it cannot be shadowed: the
    per-connection route is ``/{server_name}`` and needs a non-empty segment, so
    there is no ordering to get wrong. A reserved alias would have needed one,
    and a name a user could pick would have been a collision waiting to happen.
    """
    if store is None:
        raise _refused(
            503, "invalid_request_error", "MCP is unavailable without a configured database."
        )
    identity = await _authorize_fence(authorization, store)
    return await _host_post(
        identity=identity, store=store, request=request, authorization=authorization
    )


@router.get("")
async def mcp_host_get() -> Any:
    """Refuse the server-initiated stream, and say why.

    This surface is request/response: it merges what its connections offer and
    answers questions about them. A long-lived stream would have to relay
    notifications from servers whose sessions it does not hold open between
    calls, and answering 200 with a stream that never carries anything is worse
    than saying so.
    """
    return JSONResponse(
        _error_reply(
            None,
            "this endpoint answers requests only; it does not open a server-initiated stream",
        ),
        status_code=405,
    )


@router.post("/{server_name}")
async def mcp_post(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: _McpRelayStorage | None = Depends(get_storage),
) -> Any:
    """Relay one MCP request, refusing tools the session's selection does not cover."""
    try:
        authorized = await _authorize(server_name, authorization, store)
    except _BindingDisabled as exc:
        # Keep the existing JSON-RPC refusal for disabled selections, including
        # housekeeping requests, without resolving or refreshing credentials.
        try:
            payload = json.loads(await request.body())
        except ValueError:
            payload = None
        request_id = payload.get("id") if isinstance(payload, dict) else None
        return JSONResponse(_error_reply(request_id, str(exc.detail)))
    assert store is not None
    body = await request.body()

    held = await _capability_decision(
        authorized,
        store,
        body,
        attempt_id=_attempt_id_of(authorization),
        lease_generation=_lease_of(authorization),
        request=request,
    )
    if held is not None:
        await _finish_dispatches(authorized, store, status="failed")
        return JSONResponse(held, status_code=200)

    server = authorized.server
    plan = plan_request(body, server)
    if plan.rejected:
        logger.warning(
            "agent_mcp_tool_blocked",
            extra={
                "event": "agent_mcp_tool_blocked",
                "job_id": authorized.job_id,
                "server": server.name,
                "tools": list(plan.blocked_tools),
            },
        )
        # 200 with a JSON-RPC error, which is what the protocol calls for: the
        # HTTP hop succeeded and the *call* failed. Answering 403 here would
        # look like a broken connection to the CLI and take the whole server
        # down for the rest of the job.
        await _finish_dispatches(authorized, store, status="failed")
        return JSONResponse(plan.rejection, status_code=200)

    try:
        response = await _proxy(
            request, server=server, job_id=authorized.job_id, method="POST", body=body
        )
    except BaseException:
        await asyncio.shield(_finish_dispatches(authorized, store, status="outcome_unknown"))
        raise
    if isinstance(response, StreamingResponse) and authorized.dispatches:
        original = response.body_iterator

        async def observed_stream() -> AsyncIterator[Any]:
            try:
                async for chunk in original:
                    yield chunk
            except BaseException:
                await asyncio.shield(
                    _finish_dispatches(authorized, store, status="outcome_unknown")
                )
                raise
            else:
                await _finish_dispatches(authorized, store, status="completed")

        response.body_iterator = observed_stream()
    else:
        await _finish_dispatches(
            authorized, store, status="failed" if response.status_code >= 400 else "completed"
        )
    return response


@router.get("/{server_name}")
async def mcp_get(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: _McpRelayStorage | None = Depends(get_storage),
) -> Any:
    """Open the server-to-client stream for an established session."""
    authorized = await _authorize(server_name, authorization, store)
    return await _proxy(request, server=authorized.server, job_id=authorized.job_id, method="GET")


@router.delete("/{server_name}")
async def mcp_delete(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: _McpRelayStorage | None = Depends(get_storage),
) -> Any:
    """Terminate an MCP session."""
    authorized = await _authorize(server_name, authorization, store)
    return await _proxy(
        request, server=authorized.server, job_id=authorized.job_id, method="DELETE"
    )


def _attempt_id_of(authorization: str | None) -> int | None:
    """The attempt a relayed request belongs to, for the invocation record."""
    try:
        claims = parse_worker_token(_bearer(authorization))
    except (InvalidAgentToken, HTTPException):
        return None
    attempt = claims.get("attempt_id")
    return int(attempt) if attempt is not None else None


def _lease_of(authorization: str | None) -> int:
    """The lease generation a relayed request was minted under."""
    try:
        claims = parse_worker_token(_bearer(authorization))
    except (InvalidAgentToken, HTTPException):
        return 0
    return int(claims.get("lease_generation") or 0)


__all__ = ["router"]
