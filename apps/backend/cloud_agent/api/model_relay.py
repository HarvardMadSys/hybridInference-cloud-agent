"""The model relay: a sandbox's agent calls a provided model without holding its key.

A connection the deployment provides keeps its key in the control plane. A
member's CLI on one is told its provider is this relay (the connection's own
path on the host's model tunnel) and holds a residency's ``amd.`` placeholder
(:func:`cloud_agent.tokens.mint_residency_model_token`) as its API key, which
no provider accepts. On every request this relay resolves the placeholder to
the running turn it speaks for, checks the turn's owner may send this model
another request today, and forwards the request to the connection's own
endpoint with the deployment's key.

**The placeholder is fenced like the other relays' credentials.** It buys
something only while its residency is the chat's hot one and is serving a
running, leased, unstopped turn on an enrolled host
(``OperatorModelRepository.resolve_relay_caller``), and only for the provided
connection that turn runs on, at the revision the member's process was started
on. Stop, a superseded attempt, a rotated residency, a removed host, and a
connection disabled, deleted or moved to another Base URL all take effect on
the next request, with nothing to revoke anywhere.

**It is not a proxy.** The destination is the connection's Base URL and
nothing a request says changes it. Only the inference and listing paths an
agent CLI calls under that URL are forwarded, matched whole
(:func:`cloud_agent.domain.operator_models.relay_route`), and only for a model
the connection lists. A credential that is not a placeholder is refused, not
passed on: an owner's own key never comes this way.

**Allowances are counted here, outside the sandbox.** One statement decides
and counts a request (``count_request``); a request the provider never served
(unreachable, 5xx, 429) is taken back. Two caps on requests in flight, per
owner and per model, are kept in this process.

**Nothing of a request or an answer is stored or logged.** A request body is
read once to find the model it names and forwarded unchanged; an answer is
streamed back as it arrives, the provider's errors included.

See ``docs/agents/specs/2026-11-12-platform-model-relay.zh-CN.md``.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from cloud_agent.api.deps import get_storage
from cloud_agent.application.model_connection_check import guard_base_url
from cloud_agent.application.model_connection_stream import CheckStop
from cloud_agent.application.model_connections import operator_connection_cipher
from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.crypto.domain_cipher import settings_root_key
from cloud_agent.domain.operator_models import (
    end_user_tag,
    is_messages_path,
    relay_route,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.transports import pinned_transport
from cloud_agent.tokens import InvalidAgentToken, parse_residency_model_token
from cloud_agent_shared.shared_protocol import is_residency_model_credential

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from datetime import date

    from cloud_agent.ports import OperatorModelRepository

logger = get_logger("cloud_agent.model_relay")

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
#: Where a client may put its credential; both are replaced, never forwarded.
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key"})
#: An answer can stream for minutes; a stalled connection still ends.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0)
#: The largest request body read and forwarded. A model request carries the
#: whole conversation, images included; a provider refuses far smaller ones.
MAX_BODY_BYTES = 32 * 1024 * 1024
#: Upstream answers after which a counted request is taken back: the provider
#: did not serve it, and the CLI will send it again.
_REFUNDED_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

#: The Anthropic error type a refusal of each status is answered as.
_ANTHROPIC_TYPES = {
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    400: "invalid_request_error",
}


class _RelayStorage(Protocol):
    operator_models: OperatorModelRepository


class _Refused(Exception):
    """This request is not forwarded; ``code`` names why in the relay's vocabulary."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class Slots:
    """Requests in flight, per owner and per model, in this process.

    Not shared between relay processes: each one enforces the caps on its own
    requests. Asyncio-only, so taking and giving back need no lock.
    """

    def __init__(self) -> None:
        """Start with nothing in flight."""
        self._held: Counter[tuple[str, ...]] = Counter()

    def take(self, keys: list[tuple[tuple[str, ...], int | None]]) -> bool:
        """Take one slot under every key, or none when any is at its cap."""
        if any(cap is not None and self._held[key] >= cap for key, cap in keys):
            return False
        for key, _cap in keys:
            self._held[key] += 1
        return True

    def give(self, keys: list[tuple[tuple[str, ...], int | None]]) -> None:
        """Give back what :meth:`take` took."""
        for key, _cap in keys:
            self._held[key] -= 1
            if self._held[key] <= 0:
                del self._held[key]

    def held(self, key: tuple[str, ...]) -> int:
        """How many requests are in flight under ``key``."""
        return self._held[key]


#: This process's slots. A test replaces it through :func:`get_slots`.
_SLOTS = Slots()


def get_slots() -> Slots:
    """Dependency: the process's in-flight counters."""
    return _SLOTS


def get_relay_client(request: Request) -> httpx.AsyncClient | None:
    """Dependency: the listener's upstream client; ``None`` opens one for the request."""
    return getattr(request.app.state, "model_relay_http", None)


def get_endpoint_guard() -> Callable[[str], Awaitable[None]]:
    """Dependency: the check every upstream dial passes first.

    The control plane's own rule for an inference endpoint: public IPv4 only,
    and the dial that follows is pinned to the addresses checked.
    """
    return guard_base_url


def relay_client() -> httpx.AsyncClient:
    """The client the relay forwards with: pinned dials, no proxy, no redirects, no cookies."""
    return httpx.AsyncClient(
        transport=pinned_transport(),
        follow_redirects=False,
        trust_env=False,
        timeout=_UPSTREAM_TIMEOUT,
    )


@dataclass
class _Admitted:
    """One request this relay will forward, and what to undo if the provider does not serve it."""

    store: _RelayStorage
    slots: Slots
    caller: dict[str, Any]
    model: str | None = None
    keys: list[tuple[tuple[str, ...], int | None]] = field(default_factory=list)
    counted_day: date | None = None
    released: bool = False

    @property
    def connection(self) -> dict[str, Any]:
        """The provided connection the turn runs on."""
        connection: dict[str, Any] = self.caller["connection"]
        return connection

    def release(self) -> None:
        """Give the in-flight slots back, once."""
        if not self.released:
            self.released = True
            self.slots.give(self.keys)

    async def refund(self) -> None:
        """Take the counted request back, once: the provider did not serve it."""
        day, self.counted_day = self.counted_day, None
        if day is None or self.model is None:
            return
        try:
            await self.store.operator_models.refund_operator_request(
                connection_id=str(self.connection["id"]),
                user_id=str(self.caller["user_id"]),
                model=self.model,
                day=day,
            )
        except Exception:
            # An allowance one request short is the owner's loss, not a reason
            # to fail the answer being relayed.
            logger.warning(
                "agent_model_relay_refund_failed",
                extra={"event": "agent_model_relay_refund_failed"},
                exc_info=True,
            )


def _refusal(raw_path: str, refused: _Refused) -> Response:
    """A refusal in the error dialect of the API the path belongs to."""
    body: dict[str, Any]
    if is_messages_path(raw_path):
        body = {
            "type": "error",
            "error": {
                "type": _ANTHROPIC_TYPES.get(refused.status, "api_error"),
                "message": refused.message,
                "code": refused.code,
            },
        }
    else:
        body = {
            "error": {
                "message": refused.message,
                "type": "cloud_agent_model_relay",
                "param": None,
                "code": refused.code,
            }
        }
    headers = {"Retry-After": str(refused.retry_after)} if refused.retry_after else {}
    return JSONResponse(body, status_code=refused.status, headers=headers)


def _presented(request: Request) -> tuple[str, str] | None:
    """The credential a request carries and how: ``("bearer" | "x-api-key", value)``."""
    key = (request.headers.get("x-api-key") or "").strip()
    if key:
        return "x-api-key", key
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return "bearer", value.strip()
    return None


async def _bounded_body(request: Request) -> bytes:
    """The request body, refused past :data:`MAX_BODY_BYTES` before it is all read."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _Refused(413, "request_too_large", "The request body is too large to relay.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _Refused(413, "request_too_large", "The request body is too large to relay.")
        chunks.append(chunk)
    return b"".join(chunks)


def _named_model(body: bytes) -> str | None:
    """The top-level ``model`` of a JSON request body; ``None`` when it names none."""
    try:
        document = json.loads(body)
    except ValueError:
        return None
    model = document.get("model") if isinstance(document, dict) else None
    return model if isinstance(model, str) and model else None


def _live_connection(caller: dict[str, Any]) -> dict[str, Any]:
    """The provided connection this residency's process may still use, or refuse."""
    connection = caller.get("connection")
    current = (
        connection is not None
        and connection.get("deleted_at") is None
        and connection.get("enabled")
        and caller.get("bound_revision") == connection.get("credential_revision")
    )
    if not current:
        raise _Refused(
            403,
            "provided_unavailable",
            "This deployment no longer provides the model source this task runs on.",
        )
    assert connection is not None
    return connection


async def _admit_inference(admitted: _Admitted, model: str | None) -> None:
    """Check the model, take the in-flight slots and count the request, or refuse."""
    connection, caller = admitted.connection, admitted.caller
    if model is None:
        raise _Refused(400, "invalid_request", "The request body must be JSON naming a model.")
    allowance = caller.get("allowance")
    if model not in connection["models"] or allowance is None or not allowance.offered:
        raise _Refused(
            403, "model_not_allowed", "This deployment does not provide that model to this task."
        )
    if allowance.exhausted:
        raise _exhausted()
    connection_id, user_id = str(connection["id"]), str(caller["user_id"])
    keys: list[tuple[tuple[str, ...], int | None]] = [
        (("user", connection_id, user_id), int(connection["user_concurrency"])),
        (("model", connection_id, model), caller.get("global_concurrency")),
    ]
    if not admitted.slots.take(keys):
        raise _Refused(
            429,
            "concurrency_limited",
            "Too many requests to this model are in progress. Retry shortly.",
            retry_after=1,
        )
    admitted.keys, admitted.model = keys, model
    try:
        counted = await admitted.store.operator_models.count_operator_request(
            connection_id=connection_id, user_id=user_id, model=model
        )
    except BaseException:
        admitted.release()
        raise
    if counted["outcome"] != "counted":
        admitted.release()
        if counted["outcome"] == "exhausted":
            raise _exhausted()
        raise _Refused(
            403, "model_not_allowed", "This deployment does not provide that model to this task."
        )
    admitted.counted_day = counted["day"]


def _exhausted() -> _Refused:
    return _Refused(
        403,
        "allowance_exhausted",
        "Today's requests for this model on this deployment are used up. "
        "The count starts over at 00:00 UTC.",
    )


def _upstream_headers(
    request: Request, *, style: str, api_key: str, connection: dict[str, Any], user_id: str
) -> dict[str, str]:
    """The client's headers with the deployment's key in place of the placeholder."""
    tag_header = (connection.get("end_user_header") or "").lower()
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _HOP_HEADERS
        and name.lower() not in _CREDENTIAL_HEADERS
        and name.lower() != tag_header
    }
    if style == "x-api-key":
        headers["x-api-key"] = api_key
    else:
        headers["authorization"] = f"Bearer {api_key}"
    if tag_header:
        headers[tag_header] = end_user_tag(settings_root_key(), user_id)
    return headers


def _downstream_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _HOP_HEADERS or name.lower() == "content-length"
    }


@router.api_route("/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def relay(
    path: str,
    request: Request,
    store: _RelayStorage | None = Depends(get_storage),
    http: httpx.AsyncClient | None = Depends(get_relay_client),
    slots: Slots = Depends(get_slots),
    guard: Callable[[str], Awaitable[None]] = Depends(get_endpoint_guard),
) -> Response:
    """Forward one model request with the deployment's key, or refuse it."""
    del path  # The route is decided from the path exactly as it was sent.
    raw_path = (request.scope.get("raw_path") or b"/").split(b"?", 1)[0].decode("latin-1")
    started = time.monotonic()
    admitted: _Admitted | None = None
    try:
        admitted, style, body = await _admit(request, store, slots, raw_path)
        response = await _forward(request, http, guard, admitted, style, body, raw_path)
    except _Refused as refused:
        if admitted is not None:
            admitted.release()
            await admitted.refund()
        _log(admitted, request, raw_path, refused.status, started, refused.code)
        return _refusal(raw_path, refused)
    except BaseException:
        if admitted is not None:
            admitted.release()
        raise
    _log(admitted, request, raw_path, response.status_code, started, None)
    return response


async def _admit(
    request: Request, store: _RelayStorage | None, slots: Slots, raw_path: str
) -> tuple[_Admitted, str, bytes]:
    """Resolve the placeholder to its turn and settle whether this request may go on."""
    presented = _presented(request)
    if presented is None or not is_residency_model_credential(presented[1]):
        raise _Refused(
            401, "invalid_credential", "This relay accepts only a task's own credential."
        )
    try:
        residency = parse_residency_model_token(presented[1])
    except InvalidAgentToken:
        raise _Refused(
            401, "invalid_credential", "This credential is not one this relay issued."
        ) from None
    if store is None:
        raise _Refused(503, "relay_unavailable", "The model relay has no database configured.")
    body = await _bounded_body(request) if request.method == "POST" else b""
    model = _named_model(body) if body else None
    caller = await store.operator_models.resolve_model_relay_caller(
        thread_id=residency["thread_id"],
        residency_generation=residency["residency_generation"],
        model=model,
    )
    if caller is None:
        raise _Refused(
            401, "invalid_credential", "This chat has no running turn, so it cannot reach a model."
        )
    admitted = _Admitted(store=store, slots=slots, caller=caller)
    connection = _live_connection(caller)
    route = relay_route(str(connection["base_url"]), request.method, raw_path)
    if route is None:
        raise _Refused(404, "not_found", "This relay does not forward that path.")
    if route.inference:
        await _admit_inference(admitted, model)
    return admitted, presented[0], body


async def _forward(
    request: Request,
    http: httpx.AsyncClient | None,
    guard: Callable[[str], Awaitable[None]],
    admitted: _Admitted,
    style: str,
    body: bytes,
    raw_path: str,
) -> Response:
    """Send the request to the connection's endpoint and stream its answer back."""
    connection = admitted.connection
    base_url = str(connection["base_url"])
    try:
        api_key = operator_connection_cipher().decrypt(str(connection["api_key_ciphertext"]))
    except CredentialCipherError:
        raise _Refused(
            503, "relay_unavailable", "This deployment cannot read its provider key."
        ) from None
    unreachable = _Refused(
        502, "provider_unreachable", "The model provider could not be reached from the relay."
    )
    try:
        await guard(base_url)
    except CheckStop:
        raise unreachable from None
    origin = httpx.URL(base_url).copy_with(raw_path=b"/")
    query = request.url.query
    target = raw_path + (f"?{query}" if query else "")
    client = http if http is not None else relay_client()
    upstream = client.build_request(
        request.method,
        origin.copy_with(raw_path=target.encode("latin-1")),
        headers=_upstream_headers(
            request,
            style=style,
            api_key=api_key,
            connection=connection,
            user_id=str(admitted.caller["user_id"]),
        ),
        content=body if request.method == "POST" else None,
        timeout=_UPSTREAM_TIMEOUT,
    )

    async def close() -> None:
        admitted.release()
        await answer.aclose()
        if http is None:
            await client.aclose()

    try:
        answer = await client.send(upstream, stream=True, follow_redirects=False)
    except httpx.HTTPError:
        if http is None:
            await client.aclose()
        raise unreachable from None
    try:
        await _settle(admitted, answer.status_code)
    except _Refused:
        await close()
        raise
    return StreamingResponse(
        _released_with(answer.aiter_raw(), admitted),
        status_code=answer.status_code,
        headers=_downstream_headers(answer),
        background=BackgroundTask(close),
    )


async def _settle(admitted: _Admitted, status: int) -> None:
    """Act on the provider's status before its answer is relayed.

    A 401 is the provider refusing the deployment's key, which the member's
    CLI must not read as its own credential being wrong: it is answered as a
    502 and remembered for the administrator. A request the provider did not
    serve is taken back from the allowance.
    """
    connection = admitted.connection
    connection_id = str(connection["id"])
    if status == 401:
        await admitted.store.operator_models.mark_operator_key_refused(
            connection_id=connection_id, refused=True
        )
        logger.error(
            "agent_model_relay_key_refused",
            extra={"event": "agent_model_relay_key_refused", "connection_id": connection_id},
        )
        raise _Refused(
            502,
            "provider_refused_credential",
            "The model provider refused this deployment's key. Its administrator has to fix it.",
        )
    if status in _REFUNDED_STATUSES:
        await admitted.refund()
    elif status < 400 and connection.get("key_refused_at") is not None:
        await admitted.store.operator_models.mark_operator_key_refused(
            connection_id=connection_id, refused=False
        )


async def _released_with(stream: AsyncIterator[bytes], admitted: _Admitted) -> AsyncIterator[bytes]:
    """Relay ``stream``, giving the in-flight slots back when it ends or its client leaves."""
    try:
        async for chunk in stream:
            yield chunk
    finally:
        admitted.release()


def _log(
    admitted: _Admitted | None,
    request: Request,
    raw_path: str,
    status: int,
    started: float,
    refusal: str | None,
) -> None:
    """One structured line per request: who, which model, what happened. Never content."""
    caller = admitted.caller if admitted is not None else {}
    logger.info(
        "agent_model_relay_request",
        extra={
            "event": "agent_model_relay_request",
            "job_id": caller.get("job_id"),
            "thread_id": caller.get("thread_id"),
            "user_id": caller.get("user_id"),
            "connection_id": caller.get("model_connection_id"),
            "model": admitted.model if admitted is not None else None,
            "method": request.method,
            "route": raw_path.rsplit("/", 1)[-1][:32],
            "status": status,
            "refusal": refusal,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )


__all__ = [
    "MAX_BODY_BYTES",
    "Slots",
    "get_endpoint_guard",
    "get_relay_client",
    "get_slots",
    "relay_client",
    "router",
]
