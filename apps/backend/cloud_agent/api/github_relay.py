"""The GitHub relay: git and ``gh`` in a sandbox speak as the owner, without the owner's token.

A sandbox resolves ``github.com`` and ``api.github.com`` to this relay and
trusts a certificate for those two names that only this deployment can sign
(the relay's listener terminates TLS). git and ``gh`` there hold a
residency's ``agh.`` placeholder (:func:`cloud_agent.tokens.mint_residency_github_token`),
which GitHub itself refuses. This relay exchanges it, on every request, for
the conversation owner's own GitHub App user token, and forwards the request
to GitHub.

**The placeholder is fenced like the MCP relay's credential.** It names a
residency, and buys something only while that residency is the chat's hot one
and is serving a running, leased, unstopped turn on an enrolled host
(``GitHubRelayRepository.resolve_github_caller``). Stop, a superseded attempt,
a rotated residency or a removed host take effect on the next request, with
nothing to revoke.

**The owner's token is used only for the conversation's own repository.**
GitHub cannot narrow a user token to one repository, so
:mod:`cloud_agent.github_relay_policy` does: a request about any other
repository, or any other page, goes to GitHub with no credential at all, and
the repository's administration, secrets, tags and branch deletion are refused.

**Name resolution is a convenience, not the boundary.** A sandbox may reach
GitHub's real addresses over the public egress, but it holds no GitHub
credential to reach them with: the only one is the placeholder, and only this
relay accepts it.

**Credentials the sandbox brought itself pass through untouched.** An owner
may deliberately inject a token of their own through an environment; that
request is forwarded as sent, exactly as it would have gone directly.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from cloud_agent.api.deps import get_agent_app_credentials, get_http_client, get_storage
from cloud_agent.github_relay_policy import (
    API_HOST,
    MAX_PUSH_COMMAND_BYTES,
    RELAY_HOSTS,
    WEB_HOST,
    Decision,
    Outcome,
    PushRefused,
    check_push_commands,
    check_ref_creation,
    decide,
    split_push_commands,
)
from cloud_agent.github_user_token import (
    UserTokenUnavailable,
    forget_revoked_token,
    user_access_token,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.tokens import InvalidAgentToken, parse_residency_github_token
from cloud_agent_shared.shared_protocol import is_residency_github_credential

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.github_user_token import LiveUserToken
    from cloud_agent.ports import GitHubRelayRepository, SourceControlRepository

logger = get_logger("cloud_agent.github_relay")

router = APIRouter()

#: Never forwarded in either direction: they describe this hop, not the request.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

#: A push of a large repository can take minutes either way; a stalled
#: connection still ends.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0)
#: The body of ``POST /pulls`` and ``POST /git/refs`` is a small JSON document.
_MAX_JSON_BODY = 1 << 20


class _RelayStorage(Protocol):
    github_relay: GitHubRelayRepository
    source_control: SourceControlRepository


def _refusal(host: str, status: int, message: str) -> Response:
    """A refusal git or ``gh`` will show the agent as it is."""
    if host == API_HOST:
        return JSONResponse(
            {"message": message, "documentation_url": "https://freeinference.org/"},
            status_code=status,
        )
    return Response(message + "\n", status_code=status, media_type="text/plain")


def _pkt(text: str) -> bytes:
    data = text.encode()
    return f"{len(data) + 4:04x}".encode() + data


def _push_refusal(message: str) -> Response:
    """Refuse a push the way git reports a remote's refusal: ``remote error: ...``."""
    return Response(
        _pkt(f"ERR {message}"),
        status_code=200,
        media_type="application/x-git-receive-pack-result",
    )


def _presented_credential(header: str | None) -> str | None:
    """The credential a request carries, from ``gh`` (token) or git (Basic)."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()
    value = value.strip()
    if scheme in {"bearer", "token"}:
        return value or None
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
        user, _, password = decoded.partition(":")
        return password or user or None
    return None


def _unsafe_path(request: Request, host: str) -> bool:
    """Refuse a path that could name one repository here and another at GitHub.

    The decision reads the decoded path, and GitHub reads the encoded one. They
    agree on which repository a request is about as long as the segments that
    name it (``owner/repo`` on github.com, ``repos/owner/repo`` on the API)
    carry no escapes, and no segment is a dot segment. Later segments may be
    escaped: a branch named ``feature/x`` is sent as ``feature%2Fx``.
    """
    decoded = request.scope.get("path") or ""
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        return True
    raw = (request.scope.get("raw_path") or b"").split(b"?", 1)[0]
    naming = 4 if host == API_HOST else 3
    return any(b"%" in segment for segment in raw.split(b"/")[:naming])


def _upstream_headers(request: Request, *, authorization: str | None) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _HOP_HEADERS and name.lower() != "authorization"
    }
    if authorization is not None:
        headers["authorization"] = authorization
    return headers


def _downstream_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _HOP_HEADERS or name.lower() == "content-length"
    }


def _authorization_for(host: str, token: str) -> str:
    """Git authenticates with Basic, the REST API with a bearer token."""
    if host == WEB_HOST:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return f"Basic {basic}"
    return f"Bearer {token}"


async def _checked_push_body(request: Request) -> tuple[AsyncIterator[bytes] | None, str | None]:
    """Read a push's ref updates, check them, then forward the body as it came.

    The body is forwarded byte for byte; only a decompressed copy of its head
    is read, so a gzip-encoded push is checked without being re-encoded.
    Returns the body to forward, or the reason the push is refused.
    """
    stream = request.stream()
    gzip = request.headers.get("content-encoding", "").lower() == "gzip"
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzip else None
    received: list[bytes] = []
    plain = b""
    async for chunk in stream:
        received.append(chunk)
        try:
            plain += inflater.decompress(chunk) if inflater is not None else chunk
        except zlib.error:
            return None, "the push body is not valid gzip"
        try:
            split = split_push_commands(plain)
        except PushRefused as exc:
            return None, str(exc)
        if split is not None:
            try:
                check_push_commands(split[0])
            except PushRefused as exc:
                return None, str(exc)
            break
        if len(plain) > MAX_PUSH_COMMAND_BYTES:
            return None, "the push names more ref updates than this session may send"
    else:
        return None, "the push ended before its ref updates did"

    async def body() -> AsyncIterator[bytes]:
        for chunk in received:
            yield chunk
        async for chunk in stream:
            yield chunk

    return body(), None


async def _bounded_json(request: Request) -> Any:
    body = await request.body()
    if len(body) > _MAX_JSON_BODY:
        raise PushRefused("the request body is too large")
    try:
        return json.loads(body or b"{}")
    except ValueError as exc:
        raise PushRefused("the request body is not JSON") from exc


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def relay(  # noqa: PLR0912 — one decision per branch, read top to bottom.
    path: str,
    request: Request,
    store: _RelayStorage | None = Depends(get_storage),
    app_credentials: Any = Depends(get_agent_app_credentials),
    http: httpx.AsyncClient | None = Depends(get_http_client),
) -> Response:
    """Forward one request to GitHub as the conversation's owner, as nobody, or not at all."""
    del path  # The decision reads the decoded path from the request itself.
    host = (request.headers.get("host") or "").split(":", 1)[0].lower()
    if host not in RELAY_HOSTS:
        return _refusal(host, 421, "This relay serves github.com and api.github.com only.")
    if _unsafe_path(request, host):
        return _refusal(host, 400, "Encoded or dot path segments are not relayed.")

    presented = _presented_credential(request.headers.get("authorization"))
    if presented is not None and not is_residency_github_credential(presented):
        # The owner's own credential, injected on purpose: as sent.
        return await _forward(
            request, http, host=host, authorization=request.headers.get("authorization")
        )
    if presented is None:
        # git asks its credential helper only after GitHub says it must.
        return await _forward(request, http, host=host, authorization=None)

    try:
        residency = parse_residency_github_token(presented)
    except InvalidAgentToken:
        return _refusal(host, 401, "This GitHub credential is not one this relay issued.")
    if store is None:
        return _refusal(host, 503, "GitHub is unavailable without a configured database.")
    caller = await store.github_relay.resolve_github_caller(
        thread_id=residency["thread_id"],
        residency_generation=residency["residency_generation"],
    )
    if caller is None:
        return _refusal(
            host, 401, "This chat has no running turn, so it cannot reach GitHub as its owner."
        )

    decision = decide(
        host=host,
        method=request.method,
        path=request.scope.get("path") or "",
        query=request.url.query,
        session_repo=caller.get("repo"),
    )
    if decision.outcome is Outcome.REFUSE:
        logger.info(
            "agent_github_relay_refused",
            extra={
                "event": "agent_github_relay_refused",
                "thread_id": caller["thread_id"],
                "job_id": caller["job_id"],
                "method": request.method,
                "host": host,
            },
        )
        if decision.inspect_push:
            return _push_refusal(decision.reason)
        return _refusal(host, 403, decision.reason)
    if decision.outcome is Outcome.ANONYMOUS:
        return await _forward(request, http, host=host, authorization=None)

    if app_credentials is None:
        return _refusal(host, 503, "GitHub is not configured on this deployment.")
    try:
        token = await user_access_token(
            str(caller["user_id"]), store=store.source_control, app_credentials=app_credentials
        )
    except UserTokenUnavailable:
        return _refusal(host, 503, "The owner's GitHub token is being renewed. Try again.")
    if token is None:
        return _refusal(
            host,
            403,
            "The owner of this chat has not connected GitHub, or must reconnect it, "
            "in FreeInference before this chat can act on GitHub as them.",
        )
    return await _forward_as_owner(request, http, store, caller, decision, host, token)


async def _forward_as_owner(
    request: Request,
    http: httpx.AsyncClient | None,
    store: _RelayStorage,
    caller: dict[str, Any],
    decision: Decision,
    host: str,
    token: LiveUserToken,
) -> Response:
    content: AsyncIterator[bytes] | bytes | None = None
    if decision.inspect_push:
        content, refusal = await _checked_push_body(request)
        if refusal is not None:
            logger.info(
                "agent_github_relay_push_refused",
                extra={
                    "event": "agent_github_relay_push_refused",
                    "thread_id": caller["thread_id"],
                    "job_id": caller["job_id"],
                },
            )
            return _push_refusal(refusal)
    elif decision.inspect_ref_creation or decision.records_pull_request:
        try:
            document = await _bounded_json(request)
            if decision.inspect_ref_creation:
                check_ref_creation(document.get("ref") if isinstance(document, dict) else None)
        except PushRefused as exc:
            return _refusal(host, 403 if decision.inspect_ref_creation else 400, str(exc))
        content = await request.body()

    response = await _forward(
        request,
        http,
        host=host,
        authorization=_authorization_for(host, token.value),
        content=content,
        buffer=decision.records_pull_request,
    )
    if response.status_code == 401:
        # GitHub refused the owner's token outright: forget it so they reconnect.
        await forget_revoked_token(str(caller["user_id"]), token, store=store.source_control)
    if decision.records_pull_request and response.status_code == 201:
        await _record_pull_request(store, caller, response)
    return response


async def _record_pull_request(
    store: _RelayStorage, caller: dict[str, Any], response: Response
) -> None:
    try:
        created = json.loads(bytes(response.body))
        number = int(created["number"])
        url = str(created["html_url"])
        head_ref = str((created.get("head") or {}).get("ref") or "")
        base_ref = str((created.get("base") or {}).get("ref") or "")
    except (ValueError, KeyError, TypeError):
        logger.warning(
            "agent_github_relay_pull_request_unreadable",
            extra={"event": "agent_github_relay_pull_request_unreadable"},
        )
        return
    await store.github_relay.record_pull_request(
        thread_id=str(caller["thread_id"]),
        job_id=str(caller["job_id"]),
        user_id=str(caller["user_id"]),
        repo=str(caller["repo"]),
        number=number,
        url=url,
        head_ref=head_ref,
        base_ref=base_ref,
        draft=bool(created.get("draft")),
    )
    logger.info(
        "agent_github_relay_pull_request_recorded",
        extra={
            "event": "agent_github_relay_pull_request_recorded",
            "thread_id": caller["thread_id"],
            "job_id": caller["job_id"],
            "pr_number": number,
        },
    )


async def _forward(
    request: Request,
    http: httpx.AsyncClient | None,
    *,
    host: str,
    authorization: str | None,
    content: AsyncIterator[bytes] | bytes | None = None,
    buffer: bool = False,
) -> Response:
    """Send the request to GitHub and stream GitHub's answer back unchanged."""
    client = http if http is not None else httpx.AsyncClient()
    owned = http is None
    # The path exactly as the client encoded it, so GitHub reads what was sent.
    raw_path = (request.scope.get("raw_path") or b"/").split(b"?", 1)[0].decode("ascii")
    query = request.url.query
    url = httpx.URL(f"https://{host}{raw_path}" + (f"?{query}" if query else ""))
    if content is None and request.method not in {"GET", "HEAD"}:
        content = request.stream()
    upstream = client.build_request(
        request.method,
        url,
        headers=_upstream_headers(request, authorization=authorization),
        content=content,
        timeout=_UPSTREAM_TIMEOUT,
    )
    try:
        answer = await client.send(upstream, stream=True, follow_redirects=False)
    except httpx.HTTPError:
        if owned:
            await client.aclose()
        return _refusal(host, 502, "GitHub could not be reached from the relay.")

    async def close() -> None:
        await answer.aclose()
        if owned:
            await client.aclose()

    if buffer:
        try:
            body = await answer.aread()
        finally:
            await close()
        # Decoded by the read, so it no longer has the encoding or length GitHub sent.
        headers = {
            name: value
            for name, value in _downstream_headers(answer).items()
            if name.lower() not in {"content-encoding", "content-length"}
        }
        return Response(body, status_code=answer.status_code, headers=headers)
    return StreamingResponse(
        answer.aiter_raw(),
        status_code=answer.status_code,
        headers=_downstream_headers(answer),
        background=BackgroundTask(close),
    )
