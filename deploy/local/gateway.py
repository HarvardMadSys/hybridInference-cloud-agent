"""Loopback-only gateway used by the complete local development stack.

This is intentionally a separate service, not an authentication bypass in the
control plane.  It implements the same HTTP contracts a real HybridInference
gateway owns: PKCE identity, JWKS, internal account/model lookups, short-lived
inference grants, and deterministic Anthropic/OpenAI inference responses.

It is not a production gateway.  The Compose stack publishes only its edge on
127.0.0.1 and the deterministic model is there to prove the complete agent
lifecycle without requiring a paid provider credential.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

MODEL_ID = "local-demo-model"
USER_ID = "local-developer"
CLIENT_ID = "cloud-agent"
CODE_TTL_S = 60
IDENTITY_TTL_S = 600
GRANT_TTL_MAX_S = 900
CONCURRENT_FORK_PROBE_WAIT_S = 15.0
CONCURRENT_FORK_PROBE_HOLD_S = 1.0
CONCURRENT_FORK_PROBE_PREFIX = "[local-concurrency-probe:"
#: ``[local-hold:N]`` in a turn's message holds its model answer for N seconds,
#: so a person can act while that turn is still running — queue a message
#: behind it, edit or delete one, press Stop — without a real model's latency.
#: Bounded so a mistyped number cannot wedge the local stack.
LOCAL_HOLD_PREFIX = "[local-hold:"
LOCAL_HOLD_MAX_S = 120.0
#: ``[local-tool:N]`` makes a claude-code turn run a tool for N seconds first
#: (``sleep N`` through Bash), then answer. While that tool runs the turn can
#: take a message sent into it; the answer after the tool quotes any it got.
LOCAL_TOOL_PREFIX = "[local-tool:"
LOCAL_TOOL_MAX_S = 60.0
#: How Claude Code words a message the owner sent while a tool ran, in the
#: request after that tool (the pinned 2.1.220). It arrives as a ``system``
#: entry after the tool's result, not inside the latest ``user`` message.
MID_TURN_MESSAGE_LEAD = "The user sent a new message while you were working:"

app = FastAPI(title="Local development gateway", docs_url=None, redoc_url=None)

_lock = threading.Lock()
_codes: dict[str, dict[str, Any]] = {}
_grants: dict[str, dict[str, Any]] = {}
_grant_tokens: dict[str, str] = {}
_concurrency_probes: dict[str, dict[str, Any]] = {}
_completed_concurrency_probes: deque[str] = deque(maxlen=128)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int_b64(value: int) -> str:
    width = max(1, (value.bit_length() + 7) // 8)
    return _b64url(value.to_bytes(width, "big"))


_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_numbers = _private_key.public_key().public_numbers()
_jwk_core = {"e": _int_b64(_public_numbers.e), "kty": "RSA", "n": _int_b64(_public_numbers.n)}
_kid = _b64url(
    hashlib.sha256(json.dumps(_jwk_core, separators=(",", ":"), sort_keys=True).encode()).digest()
)
_jwk = {**_jwk_core, "use": "sig", "alg": "RS256", "kid": _kid}


class ContractError(Exception):
    """An error in the gateway's documented top-level wire shape."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message


@app.exception_handler(ContractError)
async def contract_error(_request: Request, exc: ContractError) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": exc.kind, "message": exc.message}}, status_code=exc.status
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _error(status: int, kind: str, message: str) -> ContractError:
    return ContractError(status, kind, message)


def _allowed_redirects() -> set[str]:
    edge_port = os.environ.get("LOCAL_EDGE_PORT", "13002").strip() or "13002"
    raw = (os.environ.get("LOCAL_IDENTITY_ALLOWED_REDIRECTS") or "").strip()
    if not raw:
        # The control plane's callback, not a page in the web app: the code is
        # redeemed server-side, so the browser never lands on a route of its
        # own. Both spellings, because a developer may reach the edge by
        # either name and this list is matched as an exact string.
        raw = (
            f"http://127.0.0.1:{edge_port}/api/v1/session/callback,"
            f"http://localhost:{edge_port}/api/v1/session/callback"
        )
    return {entry.strip() for entry in raw.split(",") if entry.strip()}


def _dispatch_token() -> str:
    return (os.environ.get("LOCAL_GATEWAY_DISPATCH_TOKEN") or "").strip()


def _require_dispatch(authorization: str | None) -> None:
    expected = _dispatch_token()
    presented = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else ""
    )
    if not expected or not presented or not secrets.compare_digest(expected, presented):
        raise _error(401, "unauthorized", "Invalid dispatch token.")


def _pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health used by the runner's real sandbox-network preflight."""
    with _lock:
        completed = sorted(_completed_concurrency_probes)
    return {
        "ok": True,
        "service": "freeinference-local-gateway",
        # ``dev.sh`` compares this with the Compose project it launched.  A
        # generic 200 response is not enough on developer machines where an
        # SSH tunnel or another local stack may already own the requested
        # loopback port.
        "stack_id": (os.environ.get("LOCAL_STACK_ID") or "").strip(),
        "model": MODEL_ID,
        "completed_concurrency_probes": completed,
    }


@app.get("/authorize", response_class=HTMLResponse)
async def authorize(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    code_challenge_method: str = "S256",
) -> HTMLResponse:
    """Render a local-only consent page instead of silently bypassing OAuth."""
    if client_id != CLIENT_ID or redirect_uri not in _allowed_redirects():
        raise _error(400, "invalid_request", "Unknown client or redirect URI.")
    if code_challenge_method != "S256" or not 43 <= len(code_challenge) <= 128:
        raise _error(400, "invalid_request", "Only PKCE S256 is supported.")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "state": state,
        }
    )
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>Local sign in</title>"
        "<style>body{font:16px system-ui;max-width:560px;margin:12vh auto;padding:24px;"
        "color:#171717}a{display:inline-block;margin-top:16px;padding:10px 16px;"
        "border-radius:8px;background:#b4232d;color:white;text-decoration:none}"
        "code{background:#f4f4f5;padding:2px 5px;border-radius:4px}</style></head><body>"
        "<h1>Local development gateway</h1>"
        "<p>This loopback-only gateway will sign you in as "
        f"<code>{html.escape(USER_ID)}</code>. No external account or provider key is used.</p>"
        f"<a href='/authorize/continue?{html.escape(query, quote=True)}'>Continue locally</a>"
        "</body></html>"
    )


@app.get("/authorize/continue")
async def authorize_continue(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    code_challenge_method: str = "S256",
) -> RedirectResponse:
    """Issue a one-time code after the explicit local consent click."""
    if client_id != CLIENT_ID or redirect_uri not in _allowed_redirects():
        raise _error(400, "invalid_request", "Unknown client or redirect URI.")
    if code_challenge_method != "S256" or not 43 <= len(code_challenge) <= 128:
        raise _error(400, "invalid_request", "Only PKCE S256 is supported.")
    code = secrets.token_urlsafe(32)
    with _lock:
        _codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "challenge": code_challenge,
            "issued_at": time.monotonic(),
        }
    separator = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}"
    return RedirectResponse(target, status_code=302)


@app.get("/v1/identity/jwks")
async def identity_jwks() -> JSONResponse:
    return JSONResponse({"keys": [_jwk]}, headers={"Cache-Control": "public, max-age=300"})


@app.post("/v1/identity/token")
async def identity_token(request: Request) -> dict[str, Any]:
    body = await request.json()
    code = str(body.get("code") or "")
    with _lock:
        record = _codes.pop(code, None)
    if (
        record is None
        or time.monotonic() - float(record["issued_at"]) > CODE_TTL_S
        or body.get("client_id") != record["client_id"]
        or body.get("redirect_uri") != record["redirect_uri"]
        or _pkce_challenge(str(body.get("code_verifier") or "")) != record["challenge"]
    ):
        raise _error(
            400,
            "invalid_grant",
            "The authorization code is invalid, expired, or already used.",
        )
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": USER_ID,
            "iss": "freeinference-local",
            "aud": "cloud-agent",
            "iat": now,
            "exp": now + IDENTITY_TTL_S,
        },
        _private_key,
        algorithm="RS256",
        headers={"kid": _kid},
    )
    return {"access_token": token, "token_type": "Bearer", "expires_in": IDENTITY_TTL_S}


@app.get("/internal/users/{user_id}/agent-access")
async def agent_access(user_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_dispatch(authorization)
    if user_id != USER_ID:
        raise _error(403, "subject_unavailable", "That user cannot be resolved.")
    return {"user_id": user_id, "allowed": True, "permissions": ["agent.use", "agent.admin"]}


@app.get("/internal/model-catalog")
async def model_catalog(
    user_id: str = Query(...), authorization: str | None = Header(None)
) -> dict[str, Any]:
    _require_dispatch(authorization)
    if user_id != USER_ID:
        raise _error(403, "subject_unavailable", "That user cannot be resolved.")
    return {
        "user_id": USER_ID,
        "role": "admin",
        "models": [MODEL_ID],
        "aliases": {},
        "reasoning_efforts": {MODEL_ID: ["low", "medium", "high", "max"]},
    }


def _public_grant(grant: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    answer = {
        key: grant[key]
        for key in (
            "grant_id",
            "user_id",
            "external_job_id",
            "external_attempt_id",
            "allowed_models",
            "expires_at",
            "revoked_at",
        )
    }
    if token is not None:
        answer["token"] = token
    return answer


@app.post("/internal/agent-grants")
async def mint_grant(request: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_dispatch(authorization)
    body = await request.json()
    if body.get("user_id") != USER_ID:
        raise _error(403, "subject_unavailable", "That user cannot be granted capability.")
    requested = body.get("allowed_models")
    allowed = [MODEL_ID] if requested is None or MODEL_ID in requested else []
    ttl = min(max(int(body.get("ttl_seconds") or 300), 1), GRANT_TTL_MAX_S)
    key = f"{body.get('external_job_id')}:{body.get('external_attempt_id')}"
    with _lock:
        existing = next(
            (
                grant
                for grant in _grants.values()
                if f"{grant['external_job_id']}:{grant['external_attempt_id']}" == key
                and grant["revoked_at"] is None
            ),
            None,
        )
        if existing is not None:
            token = next(
                value
                for value, grant_id in _grant_tokens.items()
                if grant_id == existing["grant_id"]
            )
            return _public_grant(existing, token=token)
        grant_id = f"local-grant-{secrets.token_hex(12)}"
        token = f"agr.local.{secrets.token_urlsafe(32)}"
        grant = {
            "grant_id": grant_id,
            "user_id": USER_ID,
            "external_job_id": str(body.get("external_job_id") or ""),
            "external_attempt_id": str(body.get("external_attempt_id") or ""),
            "allowed_models": allowed,
            "expires_at": _iso(_now() + timedelta(seconds=ttl)),
            "revoked_at": None,
        }
        _grants[grant_id] = grant
        _grant_tokens[token] = grant_id
    return _public_grant(grant, token=token)


@app.post("/internal/agent-grants/{grant_id}/renew")
async def renew_grant(grant_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_dispatch(authorization)
    with _lock:
        grant = _grants.get(grant_id)
        if grant is None or grant["revoked_at"] is not None:
            raise _error(404, "grant_not_live", "That grant is not live.")
        grant["expires_at"] = _iso(_now() + timedelta(seconds=300))
        return _public_grant(grant)


@app.post("/internal/agent-grants/{grant_id}/revoke")
async def revoke_grant(grant_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_dispatch(authorization)
    with _lock:
        grant = _grants.get(grant_id)
        if grant is None:
            raise _error(404, "unknown_grant", "No such grant.")
        grant["revoked_at"] = grant["revoked_at"] or _iso(_now())
        return _public_grant(grant)


def _require_grant(x_api_key: str | None, authorization: str | None, model: str) -> None:
    presented = (x_api_key or "").strip()
    if not presented and authorization:
        presented = authorization.removeprefix("Bearer ").strip()
    with _lock:
        grant = _grants.get(_grant_tokens.get(presented, ""))
        valid = bool(
            grant
            and grant["revoked_at"] is None
            and model in grant["allowed_models"]
            and datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00")) > _now()
        )
    if not valid:
        raise _error(401, "invalid_grant", "The inference grant is invalid or expired.")


def _content_text(value: Any) -> str:
    """Flatten the current provider input without treating old history as current."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "input_text", "input"):
            if key in value:
                return _content_text(value[key])
    return ""


def _current_user_text(body: dict[str, Any]) -> str:
    """Return only the latest user input across the supported provider shapes."""
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                return _content_text(message.get("content"))
    provider_input = body.get("input")
    if isinstance(provider_input, list):
        for item in reversed(provider_input):
            if isinstance(item, dict) and item.get("role") == "user":
                return _content_text(item.get("content"))
        return _content_text(provider_input[-1]) if provider_input else ""
    return _content_text(provider_input)


def _concurrency_probe_key(body: dict[str, Any]) -> str | None:
    text = _current_user_text(body)
    start = text.find(CONCURRENT_FORK_PROBE_PREFIX)
    if start < 0:
        return None
    start += len(CONCURRENT_FORK_PROBE_PREFIX)
    end = text.find("]", start)
    key = text[start:end] if end >= 0 else ""
    if not key or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in key
    ):
        return None
    return key


async def _hold_concurrent_fork_probe(body: dict[str, Any]) -> None:
    """Require both marked fork siblings to overlap at the inference boundary."""
    key = _concurrency_probe_key(body)
    if key is None:
        return
    with _lock:
        probe = _concurrency_probes.get(key)
        if probe is None:
            probe = {"event": asyncio.Event(), "arrivals": 0, "departures": 0}
            _concurrency_probes[key] = probe
        probe["arrivals"] += 1
        if probe["arrivals"] >= 2:
            _completed_concurrency_probes.append(key)
            probe["event"].set()
        event = probe["event"]
    try:
        await asyncio.wait_for(event.wait(), timeout=CONCURRENT_FORK_PROBE_WAIT_S)
    except TimeoutError as exc:
        with _lock:
            if _concurrency_probes.get(key) is probe:
                _concurrency_probes.pop(key, None)
        raise _error(
            400,
            "concurrency_probe_timeout",
            "The two local native-fork sessions did not reach inference concurrently.",
        ) from exc
    try:
        # Leave enough overlap for the owner API assertion to observe both
        # jobs RUNNING after the stronger gateway-level proof has succeeded.
        await asyncio.sleep(CONCURRENT_FORK_PROBE_HOLD_S)
    finally:
        with _lock:
            probe["departures"] += 1
            if probe["departures"] >= 2 and _concurrency_probes.get(key) is probe:
                _concurrency_probes.pop(key, None)


def _hold_seconds(body: dict[str, Any]) -> float:
    """How long a ``[local-hold:N]`` marker asks this answer to wait, or zero."""
    return _marker_seconds(body, LOCAL_HOLD_PREFIX, LOCAL_HOLD_MAX_S)


async def _hold_marked_turn(body: dict[str, Any]) -> None:
    """Keep a marked turn's model call open, so the turn stays running."""
    seconds = _hold_seconds(body)
    if seconds:
        await asyncio.sleep(seconds)


def _marker_seconds(body: dict[str, Any], prefix: str, ceiling: float) -> float:
    """Seconds a ``prefix``N``]`` marker in the latest user input names, bounded, or zero."""
    text = _current_user_text(body)
    start = text.find(prefix)
    if start < 0:
        return 0.0
    start += len(prefix)
    end = text.find("]", start)
    try:
        seconds = float(text[start:end]) if end >= 0 else 0.0
    except ValueError:
        return 0.0
    if seconds != seconds or seconds <= 0:  # NaN or not a duration
        return 0.0
    return min(seconds, ceiling)


def _tool_seconds(body: dict[str, Any]) -> float:
    """How long a ``[local-tool:N]`` turn's tool should run, or zero for no tool."""
    return _marker_seconds(body, LOCAL_TOOL_PREFIX, LOCAL_TOOL_MAX_S)


def _since_last_reply(body: dict[str, Any]) -> str:
    """Everything a request adds after the model's last reply, whatever its role."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    added: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") == "assistant":
            break
        added.append(_content_text(message.get("content")))
    return "\n".join(reversed(added))


def _messages_sent_mid_turn(body: dict[str, Any]) -> list[str]:
    """What the owner sent into the turn while its tool ran, as the CLI relays it."""
    sent: list[str] = []
    text = _since_last_reply(body)
    start = text.find(MID_TURN_MESSAGE_LEAD)
    while start >= 0:
        start += len(MID_TURN_MESSAGE_LEAD)
        end = text.find("\n\n", start)
        message = (text[start:end] if end >= 0 else text[start:]).strip()
        if message:
            sent.append(message)
        start = text.find(MID_TURN_MESSAGE_LEAD, start)
    return sent


def _anthropic_answer(body: dict[str, Any]) -> str:
    """The reply text: the default one, or one that quotes messages sent mid-turn."""
    sent = _messages_sent_mid_turn(body)
    if sent:
        quoted = "; ".join(f'"{message}"' for message in sent)
        return f"Local gateway response: the tool finished, and I also got {quoted}."
    return "Local gateway response: the agent CLI, grant, and sandbox network are working."


def _sse_event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _anthropic_tool_sse(message_id: str, seconds: float) -> list[bytes]:
    """A streamed answer that asks the CLI to run ``sleep`` through Bash first."""
    arguments = json.dumps({"command": f"sleep {seconds:g}", "description": "Local tool call"})
    return [
        _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": MODEL_ID,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 8, "output_tokens": 1},
                },
            },
        ),
        _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": f"toolu_local_{secrets.token_hex(6)}",
                    "name": "Bash",
                    "input": {},
                },
            },
        ),
        _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            },
        ),
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 16},
            },
        ),
        _sse_event("message_stop", {"type": "message_stop"}),
    ]


def _anthropic_sse(message_id: str, text: str | None = None) -> list[bytes]:
    text = text or "Local gateway response: the agent CLI, grant, and sandbox network are working."
    event = _sse_event

    return [
        event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": MODEL_ID,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 8, "output_tokens": 1},
                },
            },
        ),
        event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 16},
            },
        ),
        event("message_stop", {"type": "message_stop"}),
    ]


@app.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> Any:
    body = await request.json()
    model = str(body.get("model") or "")
    _require_grant(x_api_key, authorization, model)
    await _hold_concurrent_fork_probe(body)
    await _hold_marked_turn(body)
    message_id = f"msg_local_{secrets.token_hex(8)}"
    if body.get("stream") is False:
        return {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": MODEL_ID,
            "content": [{"type": "text", "text": "Local gateway response."}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 8, "output_tokens": 4},
        }
    seconds = _tool_seconds(body)
    if seconds:
        return StreamingResponse(
            iter(_anthropic_tool_sse(message_id, seconds)), media_type="text/event-stream"
        )
    return StreamingResponse(
        iter(_anthropic_sse(message_id, _anthropic_answer(body))), media_type="text/event-stream"
    )


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> dict[str, int]:
    body = await request.json()
    _require_grant(x_api_key, authorization, str(body.get("model") or ""))
    return {"input_tokens": 8}


def _openai_chat_events(completion_id: str) -> list[bytes]:
    text = "Local gateway response: the agent CLI and sandbox are working."
    chunks = [
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ],
        },
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    return [*(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks), b"data: [DONE]\n\n"]


@app.post("/v1/chat/completions")
async def openai_chat_completions(
    request: Request, authorization: str | None = Header(None)
) -> Any:
    body = await request.json()
    model = str(body.get("model") or "")
    _require_grant(None, authorization, model)
    await _hold_concurrent_fork_probe(body)
    await _hold_marked_turn(body)
    completion_id = f"chatcmpl-local-{secrets.token_hex(8)}"
    if body.get("stream"):
        return StreamingResponse(
            iter(_openai_chat_events(completion_id)), media_type="text/event-stream"
        )
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Local gateway response."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }


@app.post("/v1/responses")
async def openai_responses(request: Request, authorization: str | None = Header(None)) -> Any:
    body = await request.json()
    model = str(body.get("model") or "")
    _require_grant(None, authorization, model)
    await _hold_concurrent_fork_probe(body)
    await _hold_marked_turn(body)
    response_id = f"resp_local_{secrets.token_hex(8)}"
    text = "Local gateway response: the agent CLI and sandbox are working."
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": MODEL_ID,
        "output": [
            {
                "id": f"msg_{secrets.token_hex(8)}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 8, "output_tokens": 12, "total_tokens": 20},
    }
    if not body.get("stream"):
        return response

    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": {**response["output"][0], "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "sequence_number": 2,
            "item_id": response["output"][0]["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "item_id": response["output"][0]["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 4,
            "item_id": response["output"][0]["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "sequence_number": 5,
            "item_id": response["output"][0]["id"],
            "output_index": 0,
            "content_index": 0,
            "part": response["output"][0]["content"][0],
        },
        {
            # Codex persists completed output items. Text deltas and a final
            # response.completed alone can display a reply without retaining
            # it in the next native request, giving the smoke a false pass.
            "type": "response.output_item.done",
            "sequence_number": 6,
            "output_index": 0,
            "item": response["output"][0],
        },
        {"type": "response.completed", "sequence_number": 7, "response": response},
    ]
    return StreamingResponse(
        iter(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events),
        media_type="text/event-stream",
    )
