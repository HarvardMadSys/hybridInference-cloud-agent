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

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import protocol
from cloud_agent.mcp.authorization import decide, visible_capabilities
from cloud_agent.mcp.host import arguments_digest
from cloud_agent.mcp.invocations import deadline_for
from cloud_agent.mcp.models import CapabilityKind, McpServer
from cloud_agent.mcp.refresh import RefreshRefused, ensure_fresh
from cloud_agent.mcp.registry import (
    binding_from_row,
    invocation_cipher,
    resolve_connection,
    server_for,
)
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

    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/agent/mcp")

# An MCP call is a tool call: it can be a repository search or an issue fetch,
# and it is not a token stream. Generous, but bounded -- a sandbox waiting on a
# hung upstream is a job burning its wall-clock timeout for nothing.
_UPSTREAM_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 10.0

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


@dataclass(frozen=True)
class Authorized:
    """Everything one relayed request needs, resolved once.

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


def protocol_error(payload: dict[str, Any], message: str) -> dict[str, Any]:
    """Build a JSON-RPC error reply that mirrors one request's id.

    Answered as a *protocol* error rather than an HTTP failure on purpose: the
    transport hop succeeded and the call did not, which is a thing models
    handle. A 403 here would look to the CLI like a broken connection and take
    the whole server down for the rest of the job.
    """
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "error": {"code": METHOD_NOT_FOUND, "message": message},
    }


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


async def _authorize(
    alias: str,
    authorization: str | None,
    store: AgentJobStore | None,
) -> Authorized:
    """Resolve the token, the session's binding and the owner's connection."""
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
    identity = await store.resolve_model_credential(
        job_id=claims["job_id"],
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if identity is None:
        raise _refused(
            401, "invalid_request_error", "This job token is no longer valid for tool calls."
        )

    job_id = identity["job_id"]
    binding_row = next(
        (item for item in identity.get("mcp_bindings") or [] if item.get("alias") == alias),
        None,
    )
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

    connection_row = await store.get_mcp_connection(connection_id=int(binding_row["connection_id"]))
    if connection_row is None:
        raise _refused(503, "server_error", f"The {alias!r} MCP connection no longer exists.")
    if str(connection_row.get("transport")) == "stdio":
        # A local process needs a worker or a connector to run it, and neither
        # is this listener. Refused here rather than forwarded to an address
        # that does not exist, so the agent is told what is missing instead of
        # reporting a server that cannot be reached.
        logger.warning(
            "agent_mcp_stdio_not_runnable",
            extra={"event": "agent_mcp_stdio_not_runnable", "job_id": job_id, "alias": alias},
        )
        raise _refused(
            503,
            "server_error",
            f"The {alias!r} MCP connection runs a local process, which this deployment "
            "has no worker or connector for.",
        )

    try:
        # Before the credential is read, so a token that aged out during a long
        # job is renewed instead of being sent and refused.
        connection_row = await ensure_fresh(store, row=connection_row)
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

    resolved = await resolve_connection(store, row=connection_row)
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
    )


async def _capability_decision(
    authorized: Authorized,
    store: AgentJobStore,
    body: bytes,
    *,
    attempt_id: int | None,
    lease_generation: int,
) -> Any | None:
    """Decide one ``tools/call``, or return ``None`` when there is nothing to decide.

    Returns the JSON-RPC body to answer with when the call must not be
    dispatched, and ``None`` when it may proceed. A held call is recorded as a
    pending invocation and answered with a protocol error the model can read:
    a CLI-driven relay cannot block until a person answers, so the honest
    contract is "this needs approval -- ask again once it is granted", and an
    approval is consumed by the retry that matches it.
    """
    try:
        document = json.loads(body or b"{}")
    except (json.JSONDecodeError, ValueError):
        return None
    payloads = document if isinstance(document, list) else [document]
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("method") != protocol.TOOLS_CALL:
            continue
        params = payload.get("params")
        name = str(params.get("name") or "") if isinstance(params, dict) else ""
        arguments = params.get("arguments") if isinstance(params, dict) else None
        capability = next(
            (
                item
                for item in (authorized.snapshot.tools if authorized.snapshot else ())
                if item.name == name
            ),
            None,
        )
        decision = decide(
            connection=authorized.connection,
            binding=authorized.binding,
            capability=capability,
            kind=CapabilityKind.TOOL,
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
            return protocol_error(payload, decision.message or "this tool is not enabled")

        digest = arguments_digest(arguments if isinstance(arguments, dict) else None)
        approved = await store.find_approved_mcp_invocation(
            job_id=authorized.job_id,
            connection_id=authorized.connection_id,
            capability_name=name,
            arguments_digest=digest,
        )
        if approved is not None:
            claimed = await store.claim_mcp_invocation(invocation_id=int(approved["id"]))
            if claimed is not None:
                # Exactly one dispatcher ever wins this UPDATE, which is why a
                # retry storm cannot send a write twice.
                return None
        invocation = await store.create_mcp_invocation(
            job_id=authorized.job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            thread_id=authorized.thread_id,
            connection_id=authorized.connection_id,
            capability_name=name,
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
        return protocol_error(
            payload,
            f"Calling {name!r} needs your approval. It is waiting on the Integrations page; "
            "ask again once it is approved.",
        )
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


def build_upstream_client() -> httpx.AsyncClient:
    """Build the client the proxy dials upstream with.

    Redirects are not followed: a redirect would re-send this deployment's
    credential to whatever host the response named.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_UPSTREAM_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
        follow_redirects=False,
    )


async def _proxy(
    request: Request,
    *,
    server: McpServer,
    job_id: str,
    method: str,
    body: bytes = b"",
) -> Any:
    """Forward one request upstream and stream or filter the reply back."""
    client = build_upstream_client()
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
        # back into `connection_states()`, so Integrations says "connect it
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


@router.post("/{server_name}")
async def mcp_post(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Any:
    """Relay one MCP request, refusing tools the session's selection does not cover."""
    authorized = await _authorize(server_name, authorization, store)
    body = await request.body()

    held = await _capability_decision(
        authorized,
        store,  # type: ignore[arg-type] - _authorize refuses a missing store
        body,
        attempt_id=_attempt_id_of(authorization),
        lease_generation=_lease_of(authorization),
    )
    if held is not None:
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
        return JSONResponse(plan.rejection, status_code=200)

    return await _proxy(request, server=server, job_id=authorized.job_id, method="POST", body=body)


@router.get("/{server_name}")
async def mcp_get(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Any:
    """Open the server-to-client stream for an established session."""
    authorized = await _authorize(server_name, authorization, store)
    return await _proxy(request, server=authorized.server, job_id=authorized.job_id, method="GET")


@router.delete("/{server_name}")
async def mcp_delete(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
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
