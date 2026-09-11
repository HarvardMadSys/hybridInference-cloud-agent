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

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp_proxy import (
    MAX_BUFFERED_RESPONSE_BYTES,
    SseFilter,
    filter_json_body,
    plan_request,
)
from cloud_agent.mcp_registry import McpServer, registry_for_user
from cloud_agent.tokens import SCOPE_MCP, InvalidAgentToken, parse_worker_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/agent/mcp")

# An MCP call is a tool call: it can be a repository search or an issue fetch,
# and it is not a token stream. Generous, but bounded — a sandbox waiting on a
# hung upstream is a job burning its wall-clock timeout for nothing.
_UPSTREAM_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 10.0

# Forwarded from the agent to the upstream. Everything else — Authorization
# above all, but equally Cookie, X-Forwarded-*, and whatever else the CLI or an
# intermediary attaches — is dropped: the upstream sees a request this control
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


async def _authorize(
    server_name: str,
    authorization: str | None,
    store: AgentJobStore | None,
) -> tuple[McpServer, str]:
    """Resolve the token and the named server, or raise the right HTTP error."""
    token = _bearer(authorization)

    try:
        claims = parse_worker_token(token)
    except InvalidAgentToken as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "invalid_request_error", "message": str(exc)}},
        ) from exc

    # **Positive, and it has to be.** `parse_worker_token` reads a token minted
    # before scopes existed as `full`, which is the conservative reading when
    # deciding whether a signature is trustworthy and precisely the wrong one
    # here — a check written as "not full" would admit every such token. A
    # runner's full-scope token is refused too: reporting events and driving
    # terminal transitions is not a reason to be able to call somebody's tools.
    if claims.get("scope") != SCOPE_MCP:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": "This endpoint requires an MCP-scoped token.",
                }
            },
        )

    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "invalid_request_error",
                    "message": "MCP is unavailable without a configured database.",
                }
            },
        )

    # The fence, read fresh on every call. The token names an attempt; this
    # asks whether that attempt is still the job's current one, still running,
    # with an unexpired lease. So revocation is automatic — the instant the
    # reaper supersedes the attempt or the owner cancels, the same token stops
    # buying tools, with no key to remember to revoke.
    identity = await store.resolve_model_credential(
        job_id=claims["job_id"],
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "invalid_request_error",
                    "message": "This job token is no longer valid for tool calls.",
                }
            },
        )

    job_id = identity["job_id"]
    if server_name not in (identity.get("mcp_servers") or []):
        # The job's own list, not the registry's. A token is scoped to the job
        # it was minted for, and a job reaches exactly the servers it was
        # created with.
        logger.warning(
            "agent_mcp_server_not_granted",
            extra={
                "event": "agent_mcp_server_not_granted",
                "job_id": job_id,
                "server": server_name,
            },
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "permission_error",
                    "message": f"This job was not created with the {server_name!r} MCP server.",
                }
            },
        )

    # The owner's servers, not the caller's — see the module docstring. The
    # same fence row that authorized this call names them.
    registry = await registry_for_user(store, user_id=identity["user_id"])
    server = registry.get(server_name)
    if server is None:
        # Granted at creation and unreachable now: the owner disconnected the
        # server, the credential stopped decrypting, or the provider left the
        # catalogue. Not the job's fault and not a permission problem, so it is
        # reported as the server being gone rather than as a missing endpoint
        # or a refusal — and deliberately without saying which of the three it
        # was, since the sandbox has no business learning about its owner's
        # account state.
        logger.warning(
            "agent_mcp_server_unavailable",
            extra={
                "event": "agent_mcp_server_unavailable",
                "job_id": job_id,
                "server": server_name,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "server_error",
                    "message": (
                        f"The {server_name!r} MCP server is not currently connected for "
                        "this job's owner."
                    ),
                }
            },
        )
    return server, job_id


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
    """Relay one MCP request, refusing tools the allowlist does not cover."""
    server, job_id = await _authorize(server_name, authorization, store)
    body = await request.body()

    plan = plan_request(body, server)
    if plan.rejected:
        logger.warning(
            "agent_mcp_tool_blocked",
            extra={
                "event": "agent_mcp_tool_blocked",
                "job_id": job_id,
                "server": server.name,
                "tools": list(plan.blocked_tools),
            },
        )
        # 200 with a JSON-RPC error, which is what the protocol calls for: the
        # HTTP hop succeeded and the *call* failed. Answering 403 here would
        # look like a broken connection to the CLI and take the whole server
        # down for the rest of the job.
        return JSONResponse(plan.rejection, status_code=200)

    return await _proxy(request, server=server, job_id=job_id, method="POST", body=body)


@router.get("/{server_name}")
async def mcp_get(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Any:
    """Open the server-to-client stream for an established session."""
    server, job_id = await _authorize(server_name, authorization, store)
    return await _proxy(request, server=server, job_id=job_id, method="GET")


@router.delete("/{server_name}")
async def mcp_delete(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> Any:
    """Terminate an MCP session."""
    server, job_id = await _authorize(server_name, authorization, store)
    return await _proxy(request, server=server, job_id=job_id, method="DELETE")


__all__ = ["router"]
