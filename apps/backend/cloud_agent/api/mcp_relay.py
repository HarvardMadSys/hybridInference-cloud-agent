"""The MCP relay: this service's own, and the only door to its tools.

MCP belongs here. This service owns the registry, each upstream's credential,
the job that requested a server and the attempt fence that says whether that
job is still running — every input a tool-call decision needs. The gateway had
the token and none of the state, which is why the ownership amendment moved
this whole surface across.

**Two credentials, deliberately not interchangeable.** A sandbox carries an
``agr`` grant for model calls, which the gateway mints and verifies, and an
MCP credential for tools, which this service mints and verifies. A leaked
grant buys no tools; a leaked MCP credential buys no inference, no event log,
no patch and no terminal state.

**The MCP credential names a residency, not a turn.** A chat's process
outlives the turn it was started for and serves the next ones, which are other
jobs, so a per-attempt token would force a restart at every message. Its
``amr.`` credential names the chat's residency, and the fence resolves it on
every call to the turn that residency is running; with none running it buys
nothing. From there it is fenced exactly like a turn's own token.

**The scope check is positive.** It covers an attempt's ``ajt`` token, which
the claim still mints. ``parse_worker_token`` reads a token minted before
scopes existed as ``full`` — the conservative reading for signing, and exactly
the wrong one here: a check written as "not full" would admit every such
token to the tool surface. So this requires ``scope == SCOPE_MCP`` and refuses
everything else, ``full`` included. A runner has no business making tool calls.

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

**Where each part lives.** This module holds the routes and the merged
endpoint's dispatch. Resolving the caller and its bindings is in
:mod:`cloud_agent.api._mcp_relay_callers`, the decision and the approval hold
in :mod:`cloud_agent.api._mcp_relay_approval`, and rendering what goes back in
:mod:`cloud_agent.mcp.relay_surface`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from cloud_agent.api._mcp_relay_approval import decide_call, finish_dispatches
from cloud_agent.api._mcp_relay_callers import (
    authorize_fence,
    host_catalogue,
    recheck_authorized,
    recheck_fence,
    refresh_catalogue_entry,
    refused,
    residency_fence,
    resolve_live_binding,
)
from cloud_agent.api.deps import get_http_client, get_storage
from cloud_agent.domain.agent_memory import MemoryTask
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import protocol, sandbox_tools
from cloud_agent.mcp.host import HostError, SessionKey
from cloud_agent.mcp.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MAX_MESSAGE_BYTES,
    PARSE_ERROR,
)
from cloud_agent.mcp.models import CapabilityKind
from cloud_agent.mcp.relay_surface import (
    error_reply,
    initialize_result,
    prompt_entry,
    resource_entry,
    result_reply,
    tool_entry,
)
from cloud_agent.mcp.resources import (
    ResourceResolutionError,
    resource_capability,
    scope_resource_result,
    split_resource_uri,
)
from cloud_agent.mcp.runtime import get_host as _host
from cloud_agent.memory import memory_tool
from cloud_agent.providers import web_search

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.api._mcp_relay_callers import Authorized
    from cloud_agent.ports import (
        AgentMemoryRepository,
        CredentialQueries,
        McpConnectionRepository,
        WebSearchRepository,
    )


logger = get_logger(__name__)


class McpRelayStorage(Protocol):
    """The capabilities the relay's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    agent_memory: AgentMemoryRepository
    credential_queries: CredentialQueries
    mcp: McpConnectionRepository
    web_search: WebSearchRepository


router = APIRouter(prefix="/v1/agent/mcp")


_HOST_KEEPALIVE_S = 15.0


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

#: The largest request body the relay reads: the bound every JSON-RPC frame in
#: this service already has. It is enforced on the declared length, and again
#: while bytes arrive. The relay is one process every tenant shares; a body read
#: whole before measurement has already cost what the bound saves.
MAX_RELAY_BODY_BYTES = MAX_MESSAGE_BYTES

#: The most messages one JSON-RPC batch may carry. Current MCP revisions do not
#: batch at all, and no client batches tool calls in practice; the cap bounds
#: how much work one request can queue behind itself.
MAX_RELAY_BATCH_MESSAGES = 32


class _BodyRefused(Exception):
    """A request this relay will not read or run, carrying the reply that says so."""

    def __init__(self, status_code: int, message: str) -> None:
        """Build the JSON-RPC refusal, with the null id an unread request gets."""
        super().__init__(message)
        self.response = JSONResponse(
            error_reply(None, message, INVALID_REQUEST), status_code=status_code
        )


def _require_body_size(size: int) -> None:
    """Refuse a body whose declared or received size is over :data:`MAX_RELAY_BODY_BYTES`."""
    if size > MAX_RELAY_BODY_BYTES:
        raise _BodyRefused(413, f"An MCP request body is limited to {MAX_RELAY_BODY_BYTES} bytes.")


async def _read_body(request: Request) -> bytes:
    """Read a POST body under :data:`MAX_RELAY_BODY_BYTES`, or refuse it.

    A declared length over the bound is refused before anything is read. The
    bound is applied again to the bytes as they stream in, because
    ``Content-Length`` is only the sender's claim and a chunked body makes none.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit():
        _require_body_size(int(declared))
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        _require_body_size(size)
        chunks.append(chunk)
    return b"".join(chunks)


def _bounded_batch(document: Any) -> Any:
    """Return a parsed document, refusing a batch over :data:`MAX_RELAY_BATCH_MESSAGES`."""
    if isinstance(document, list) and len(document) > MAX_RELAY_BATCH_MESSAGES:
        raise _BodyRefused(
            400, f"A JSON-RPC batch is limited to {MAX_RELAY_BATCH_MESSAGES} messages."
        )
    return document


def _binding_error(request_id: Any, exc: HTTPException) -> dict[str, Any]:
    """Return a selected-connection failure without taking down the merged endpoint."""
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        detail = detail["error"].get("message")
    return error_reply(request_id, str(detail or "This MCP connection is unavailable."))


# --------------------------------------------------------------------------
# The merged host endpoint
# --------------------------------------------------------------------------
#
# One URL that presents every connection this job is bound to as a single MCP
# server, with tool names namespaced by binding alias.
#
# A runtime whose MCP entries are gated by a name-and-identity allowlist it
# cannot extend at run time needs the *name* before the job starts, and a
# per-conversation alias never is knowable then. One fixed name and one fixed
# URL is the shape that fits every runtime.
#
# It is the design's "relay as protocol host" step: the platform owns the
# protocol -- it answers ``initialize``, merges the catalogues, routes a call by
# its namespaced name and normalizes what comes back -- and every call goes
# through the one decision function above.


async def _host_session(authorized: Authorized, authorization: str | None) -> Any:
    """Open or reuse the host session for one connection, fenced to this attempt.

    The member identity is the job and attempt the token names, and the owner
    lease generation is part of the session key: a takeover produces a new key
    and leaves the superseded session to be reaped, rather than handing the new
    owner the old one's upstream state.
    """
    key = SessionKey(
        connection_id=str(authorized.connection_id),
        member=f"{authorized.job_id}:{authorized.attempt_id or 0}",
        owner_lease_generation=authorized.lease_generation,
    )
    return await _host().open(authorized.connection, secret=authorized.secret, key=key)


async def _host_post(
    *,
    identity: dict[str, Any],
    store: McpRelayStorage,
    request: Request,
    authorization: str | None,
) -> Any:
    """Dispatch one JSON-RPC message addressed to the merged surface."""
    try:
        body = await _read_body(request)
        try:
            document = _bounded_batch(json.loads(body or b"{}"))
        except (ValueError, RecursionError) as exc:
            return JSONResponse(
                error_reply(None, f"invalid JSON: {exc}", PARSE_ERROR), status_code=400
            )
    except _BodyRefused as exc:
        return exc.response

    # The native HTTP stack can give up on a completely silent request even
    # when the CLI's MCP timers cover the approval window. Stream a response
    # immediately and keep its socket alive while the original fenced call
    # waits. JSON-only callers and batches retain their response shape.
    if (
        isinstance(document, dict)
        and "id" in document
        and document.get("method")
        in {protocol.TOOLS_CALL, protocol.RESOURCES_READ, protocol.PROMPTS_GET}
        and _accepts_event_stream(request)
    ):
        return StreamingResponse(
            _host_call_stream(
                document=document,
                identity=identity,
                store=store,
                request=request,
                authorization=authorization,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    return await _host_document(
        document=document,
        identity=identity,
        store=store,
        request=request,
        authorization=authorization,
    )


def _accepts_event_stream(request: Request) -> bool:
    """Select SSE only when the caller explicitly accepts it with positive quality."""
    for offered in request.headers.get("accept", "").lower().split(","):
        media_type, *parameters = offered.strip().split(";")
        if media_type != "text/event-stream":
            continue
        quality = "1"
        for parameter in parameters:
            key, _, value = parameter.strip().partition("=")
            if key == "q":
                quality = value
        try:
            if float(quality) > 0:
                return True
        except ValueError:
            continue
    return False


async def _host_call_stream(
    *,
    document: dict[str, Any],
    identity: dict[str, Any],
    store: McpRelayStorage,
    request: Request,
    authorization: str | None,
) -> AsyncIterator[bytes]:
    """Keep one request alive without dispatching it again or retaining it after disconnect."""
    task = asyncio.create_task(
        _host_document(
            document=document,
            identity=identity,
            store=store,
            request=request,
            authorization=authorization,
        )
    )
    try:
        yield b": MCP call in progress\n\n"
        while True:
            done, _ = await asyncio.wait({task}, timeout=_HOST_KEEPALIVE_S)
            if done:
                try:
                    response = task.result()
                    body = response.body
                except HTTPException as exc:
                    body = json.dumps(_binding_error(document["id"], exc)).encode()
                except Exception:
                    logger.exception("agent_mcp_stream_call_failed")
                    body = json.dumps(
                        error_reply(
                            document["id"],
                            "MCP relay could not confirm this call's result. Check external state before retrying a write.",
                            INTERNAL_ERROR,
                        )
                    ).encode()
                yield b"event: message\ndata: " + body + b"\n\n"
                return
            yield b": MCP call in progress\n\n"
    finally:
        if not task.done():
            task.cancel()
        # Cancelling the dispatcher expires a pending approval, or marks an
        # already-dispatched write's outcome unknown. It must finish before
        # this request disappears; never leave a detached background write.
        await asyncio.gather(task, return_exceptions=True)


async def _host_document(
    *,
    document: Any,
    identity: dict[str, Any],
    store: McpRelayStorage,
    request: Request,
    authorization: str | None,
) -> Any:
    """Handle the already-bounded document for either JSON or SSE replies."""
    payloads = document if isinstance(document, list) else [document]
    replies: list[dict[str, Any]] = []
    stateless = False
    for payload in payloads:
        if not isinstance(payload, dict):
            replies.append(error_reply(None, "every message must be an object", INVALID_REQUEST))
            continue
        # Reading the body or an earlier call in this batch may have outlived
        # either fence. Keep the attempt captured at authentication throughout.
        identity = await recheck_fence(identity, store)
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
                result_reply(
                    request_id,
                    await initialize_result(payload, identity),
                    stateless=False,
                )
            )
            continue
        if method == protocol.PING:
            replies.append(result_reply(request_id, {}, stateless=stateless))
            continue
        if method == protocol.TOOLS_LIST:
            catalogue = await host_catalogue(identity, store)
            tools = [
                tool_entry(name, authorized, capability)
                for name, (authorized, capability) in sorted(
                    catalogue.items(), key=lambda item: item[0]
                )
            ]
            if web_search.configured():
                tools.append(web_search.TOOL_DEFINITION)
            # Platform tools, served here rather than by a connection. Memory is
            # offered to every task; a scope turned off refuses writes itself.
            tools.extend(memory_tool.TOOL_DEFINITIONS)
            tools.sort(key=lambda tool: str(tool["name"]))
            replies.append(result_reply(request_id, {"tools": tools}, stateless=stateless))
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
        if method == sandbox_tools.METHOD:  # the sandbox's gateway, reporting a program's tools
            replies.append(await sandbox_tools.reply(payload, identity, store))
            continue
        replies.append(error_reply(request_id, f"this surface does not implement {method!r}"))

    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(document, list) else replies[0])


async def _host_listing(
    method: str,
    *,
    request_id: Any,
    identity: dict[str, Any],
    store: McpRelayStorage,
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
        catalogue = await host_catalogue(identity, store, CapabilityKind.RESOURCE)
        return result_reply(
            request_id,
            {
                "resources": [
                    resource_entry(capability, uri) for uri, (_, capability) in catalogue.items()
                ]
            },
            stateless=stateless,
        )
    if method == protocol.RESOURCES_TEMPLATES_LIST:
        catalogue = await host_catalogue(identity, store, CapabilityKind.RESOURCE_TEMPLATE)
        return result_reply(
            request_id,
            {
                "resourceTemplates": [
                    resource_entry(capability, uri) for uri, (_, capability) in catalogue.items()
                ]
            },
            stateless=stateless,
        )
    catalogue = await host_catalogue(identity, store, CapabilityKind.PROMPT)
    return result_reply(
        request_id,
        {
            "prompts": [
                prompt_entry(presented, capability)
                for presented, (_, capability) in sorted(catalogue.items())
            ]
        },
        stateless=stateless,
    )


async def _host_fetch(  # noqa: PLR0912 complexity baseline, #771
    payload: dict[str, Any],
    *,
    method: str,
    request_id: Any,
    identity: dict[str, Any],
    store: McpRelayStorage,
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
        return error_reply(
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
            authorized = await resolve_live_binding(
                identity,
                str(binding.get("alias") or ""),
                store,
                attempt_id=identity["attempt_id"],
                lease_generation=identity["lease_generation"],
            )
            if str(authorized.connection_id) != connection_id:
                raise ResourceResolutionError("The selected resource connection changed.")
            capability = resource_capability(authorized.snapshot, authorized.binding, original_uri)
        except ResourceResolutionError as exc:
            return error_reply(request_id, str(exc))
        except HTTPException as exc:
            return _binding_error(request_id, exc)
        if capability is None:
            return error_reply(
                request_id, "That URI is not in this session's resource catalogue or templates."
            )
        kind = capability.kind
    else:
        kind = CapabilityKind.PROMPT
        catalogue = await host_catalogue(protocol.presenting(identity, key), store, kind)
        entry = catalogue.get(key)
        if entry is None:
            return error_reply(
                request_id,
                f"{key!r} is not one of this session's prompts. Call prompts/list for the current set.",
            )
        try:
            # Resources and prompts are connections only: the platform's own
            # tools are a tool surface, so this catalogue holds nothing that is
            # not a resolved connection.
            authorized, capability = await refresh_catalogue_entry(
                cast("tuple[Authorized, Any]", entry), identity, store
            )
        except HTTPException as exc:
            return _binding_error(request_id, exc)
    held = await decide_call(
        authorized,
        store,
        name=capability.name,
        arguments=settings.get("arguments")
        if not reading
        else {"uri": original_uri, "method": method},
        capability=capability,
        attempt_id=identity["attempt_id"],
        lease_generation=identity["lease_generation"],
        request_id=request_id,
        request=request,
        kind=kind,
    )
    if held is not None:
        return held

    try:
        session = await _host_session(authorized, authorization)
        await recheck_authorized(authorized, store)
        if reading:
            result = await _host().read_resource(session, uri=original_uri)
        else:
            arguments = settings.get("arguments")
            result = await _host().get_prompt(
                session,
                name=capability.name,
                arguments=arguments if isinstance(arguments, dict) else None,
            )
    except HTTPException as exc:
        await finish_dispatches(authorized, store, status="failed")
        return _binding_error(request_id, exc)
    except asyncio.CancelledError:
        await asyncio.shield(finish_dispatches(authorized, store, status="outcome_unknown"))
        raise
    except HostError as exc:
        await finish_dispatches(authorized, store, status="outcome_unknown")
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
        return error_reply(request_id, str(exc), INTERNAL_ERROR)
    await finish_dispatches(authorized, store, status="completed")
    return result_reply(
        request_id, scope_resource_result(result, authorized.connection_id), stateless=stateless
    )


async def _release_web_search(store: McpRelayStorage, *, call_id: str, job_id: str) -> None:
    """Release provider concurrency without ever replacing the call's outcome."""
    try:
        released = await store.web_search.release_web_search(call_id=call_id)
        if not released:
            logger.warning(
                "agent_web_search_release_missing",
                extra={"event": "agent_web_search_release_missing", "job_id": job_id},
            )
    except Exception:
        logger.exception(
            "agent_web_search_release_failed",
            extra={"event": "agent_web_search_release_failed", "job_id": job_id},
        )


async def _host_web_search(
    arguments: Any,
    *,
    request_id: Any,
    identity: dict[str, Any],
    store: McpRelayStorage,
    stateless: bool,
    client: Any = None,
) -> dict[str, Any]:
    """Dispatch the platform search tool without exposing its provider key."""
    search_arguments = arguments if isinstance(arguments, dict) else {}
    try:
        # Validate before consuming a shared-key admission slot. ``search`` repeats
        # this at the provider boundary so it remains safe as a standalone client.
        web_search.validate(
            search_arguments,
            provider=web_search.selected_provider(),
        )
    except web_search.WebSearchInputError as exc:
        return error_reply(request_id, str(exc), INVALID_PARAMS)

    attempt_id = identity["attempt_id"]
    lease_generation = identity["lease_generation"]
    if attempt_id is None or lease_generation <= 0:
        return result_reply(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Web search authorization expired. Retry in the active turn.",
                    }
                ],
                "isError": True,
            },
            stateless=stateless,
        )

    try:
        admission = await store.web_search.acquire_web_search(
            job_id=str(identity["job_id"]),
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            **residency_fence(identity),
        )
    except Exception:
        logger.exception(
            "agent_web_search_admission_failed",
            extra={
                "event": "agent_web_search_admission_failed",
                "job_id": identity["job_id"],
            },
        )
        return result_reply(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Web search is temporarily unavailable. Try again.",
                    }
                ],
                "isError": True,
            },
            stateless=stateless,
        )

    if admission.call_id is None:
        message = (
            "Web search is busy. Try again shortly."
            if admission.reason == "concurrency_limited"
            else "Web search usage limit reached. Try again in a minute."
        )
        if admission.reason == "stale_fence":
            message = "Web search authorization expired. Retry in the active turn."
        logger.warning(
            "agent_web_search_limited",
            extra={
                "event": "agent_web_search_limited",
                "job_id": identity["job_id"],
                "kind": admission.reason,
            },
        )
        return result_reply(
            request_id,
            {"content": [{"type": "text", "text": message}], "isError": True},
            stateless=stateless,
        )

    try:
        await recheck_fence(identity, store)
        result = await web_search.search(search_arguments, client=client)
    except HTTPException as exc:
        return _binding_error(request_id, exc)
    except web_search.WebSearchError as exc:
        logger.warning(
            "agent_web_search_failed",
            extra={
                "event": "agent_web_search_failed",
                "job_id": identity["job_id"],
                "kind": exc.kind,
            },
        )
        return result_reply(
            request_id,
            {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            },
            stateless=stateless,
        )
    else:
        return result_reply(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    }
                ]
            },
            stateless=stateless,
        )
    finally:
        await asyncio.shield(
            _release_web_search(store, call_id=admission.call_id, job_id=str(identity["job_id"]))
        )


async def _host_call(
    payload: dict[str, Any],
    *,
    request_id: Any,
    identity: dict[str, Any],
    store: McpRelayStorage,
    authorization: str | None,
    stateless: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    """Authorize and dispatch one namespaced tool call."""
    params = payload.get("params")
    name = str(params.get("name") or "") if isinstance(params, dict) else ""
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if name in memory_tool.TOOL_NAMES:
        # The owner and task come from the fence, never from the arguments. The
        # attempt goes along so the write re-checks it in its own transaction:
        # this request may have been accepted before the task was stopped.
        attempt_id = identity["attempt_id"]
        lease_generation = identity["lease_generation"]
        if attempt_id is None or lease_generation <= 0:
            result: dict[str, Any] = {
                "content": [{"type": "text", "text": "Memory authorization expired."}],
                "isError": True,
            }
        else:
            result = await memory_tool.call(
                name,
                arguments,
                task=MemoryTask(
                    user_id=str(identity["user_id"]),
                    job_id=str(identity["job_id"]),
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                    residency_generation=identity.get("residency_generation"),
                ),
                repository=store.agent_memory,
            )
        return result_reply(request_id, result, stateless=stateless)
    if name == web_search.TOOL_NAME and web_search.configured():
        return await _host_web_search(
            arguments,
            request_id=request_id,
            identity=identity,
            store=store,
            stateless=stateless,
            client=get_http_client(request) if request is not None else None,
        )
    catalogue = await host_catalogue(protocol.presenting(identity, name), store)
    entry = catalogue.get(name)
    if entry is None:
        return error_reply(
            request_id,
            f"{name!r} is not one of this session's tools. Call tools/list for the current set.",
        )
    try:
        authorized, capability = await refresh_catalogue_entry(
            cast("tuple[Authorized, Any]", entry), identity, store
        )
    except HTTPException as exc:
        return _binding_error(request_id, exc)
    held = await decide_call(
        authorized,
        store,
        name=capability.name,
        arguments=arguments,
        capability=capability,
        attempt_id=identity["attempt_id"],
        lease_generation=identity["lease_generation"],
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
        await recheck_authorized(authorized, store)
        schema = getattr(capability, "input_schema", None)
        outcome = await _host().call_tool(
            session,
            name=capability.name,
            arguments=arguments if isinstance(arguments, dict) else {},
            input_schema=schema if isinstance(schema, dict) else None,
        )
    except HTTPException as exc:
        await finish_dispatches(authorized, store, status="failed")
        return _binding_error(request_id, exc)
    except asyncio.CancelledError:
        await asyncio.shield(finish_dispatches(authorized, store, status="outcome_unknown"))
        raise
    except HostError as exc:
        await finish_dispatches(authorized, store, status="outcome_unknown")
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
        return error_reply(request_id, str(exc), INTERNAL_ERROR)
    await finish_dispatches(authorized, store, status="failed" if outcome.is_error else "completed")
    return result_reply(
        request_id,
        scope_resource_result(outcome.as_dict(), authorized.connection_id),
        stateless=stateless,
    )


@router.post("")
async def mcp_host_post(
    request: Request,
    authorization: str | None = Header(None),
    store: McpRelayStorage | None = Depends(get_storage),
) -> Any:
    """Serve every connection of one job from a single MCP endpoint."""
    if store is None:
        raise refused(
            503, "invalid_request_error", "MCP is unavailable without a configured database."
        )
    identity = await authorize_fence(authorization, store)
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
        error_reply(
            None,
            "this endpoint answers requests only; it does not open a server-initiated stream",
        ),
        status_code=405,
    )


__all__ = ["router"]
