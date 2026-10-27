"""Checking an owner's model connection the way an agent will use it.

A ``GET /models`` that answers says the key opens *something*; it says nothing
about whether the agent's CLI can stream a reply and call a tool through it,
which is all the CLI does. So a check runs exactly that, for one model and
one protocol the owner picks: a streaming request offering one tool, then a
second streaming request carrying the tool's result back. Only both succeeding
is ``supported``. A provider that answers but cannot do one of them is
``unsupported``; one that cannot be reached, refuses the key or fails is
``failed``, with a category the owner can act on.

The control plane dials an address the owner typed, so the same rules the MCP
connections use apply, plus the sandbox's own: HTTPS only; every address the
name resolves to checked, the connection pinned to the checked addresses so a
second lookup cannot answer differently; IPv4 only, since a sandbox reaches no
IPv6; no redirect followed; no proxy from the environment. Nothing the
upstream sends is echoed back -- its bodies may repeat the key or carry
anything else -- only the HTTP status and what was parsed out of the stream.

A check spends the owner's own quota upstream, so it runs only when they press
the button, one at a time per owner and within an hourly limit.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.http_body import IDENTITY_ENCODING_HEADERS, iter_response_bytes
from cloud_agent.mcp.net_policy import (
    EgressPolicy,
    EndpointRejected,
    check_addresses,
    dial_host,
    validate_endpoint,
)
from cloud_agent.mcp.transports import pinned_transport
from cloud_agent_shared.model_connections import CHAT_COMPLETIONS, MODEL_ID, RESPONSES
from cloud_agent_shared.ssh_access import SshAccessError, require_public_ipv4

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

logger = get_logger(__name__)

#: How many checks one owner may run in an hour, across replicas.
CHECKS_PER_HOUR = 20
_CONNECT_TIMEOUT_S = 10.0
_READ_TIMEOUT_S = 60.0
_CHECK_TIMEOUT_S = 150.0
#: What one streamed answer may total before the check gives up on it.
_MAX_STREAM_BYTES = 512 * 1024
_MAX_MODELS_BYTES = 1024 * 1024
_MAX_DISCOVERED = 200
_TOOL_NAME = "cloud_agent_check"
_TOOL_DESCRIPTION = "Report the check value back to the platform."
_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"value": {"type": "string", "description": "The value to report."}},
    "required": ["value"],
    "additionalProperties": False,
}

#: Owner-facing sentences, by category. None repeats anything the upstream said.
CHECK_MESSAGES: dict[str, str] = {
    "supported": "Streaming and a tool call round trip both worked.",
    "blocked_address": (
        "That address cannot be used: it resolves to a private, reserved or protected "
        "address, or has no public IPv4 address a sandbox can reach."
    ),
    "unreachable": "The provider could not be reached, or did not answer in time.",
    "tls": "The provider's TLS certificate could not be verified.",
    "redirected": "The provider redirected the request. Enter the final Base URL instead.",
    "auth": "The provider refused the API key (401/403).",
    "rate_limited": "The provider is rate limiting this key (429). Try again later.",
    "upstream_error": "The provider failed to answer (5xx). Try again later.",
    "too_large": "The provider's answer was larger than a check reads.",
    "endpoint_missing": (
        "The provider has no such endpoint or model (404). Check the Base URL, the model "
        "id, and that the provider offers this protocol."
    ),
    "request_rejected": (
        "The provider rejected a streaming request with a tool (400/422). The model or "
        "provider may not support tool calls in this protocol."
    ),
    "not_streaming": "The provider did not answer with a stream in this protocol.",
    "no_tool_call": "The model did not call the offered tool exactly once.",
    "missing_call_id": "The model's tool call carried no call id to answer it with.",
    "wrong_tool_arguments": "The model called the tool, but not with the value it was asked for.",
    "no_final_answer": "The model's answer after the tool result did not repeat that result.",
    "stream_error": "The provider reported an error, or cut the answer short, inside the stream.",
    "incomplete_stream": "The provider's stream ended before the answer finished.",
    "unexpected_status": "The provider answered with an unexpected HTTP status.",
    "invalid_response": "The provider's answer could not be read in the expected format.",
}
_FAILED = frozenset(
    {
        "blocked_address",
        "unreachable",
        "tls",
        "redirected",
        "auth",
        "rate_limited",
        "upstream_error",
        "too_large",
        "unexpected_status",
        "stream_error",
        "incomplete_stream",
    }
)


class CheckStop(Exception):
    """The check ended early, for the category named."""

    def __init__(self, category: str, *, http_status: int | None = None) -> None:
        """Carry the category and, when the upstream answered, its status."""
        super().__init__(category)
        self.category = category
        self.http_status = http_status


@dataclass
class CheckResult:
    """What a check found, as stored on the connection and shown to its owner."""

    protocol: str
    model: str
    category: str
    streaming: bool = False
    tool_call: bool = False
    final_answer: bool = False
    http_status: int | None = None
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def status(self) -> str:
        """``supported``, ``unsupported`` or ``failed``."""
        if self.category == "supported":
            return "supported"
        return "failed" if self.category in _FAILED else "unsupported"

    def as_dict(self) -> dict[str, Any]:
        """The stored and answered form."""
        return {
            "status": self.status,
            "category": self.category,
            "message": CHECK_MESSAGES[self.category],
            "protocol": self.protocol,
            "model": self.model,
            "streaming": self.streaming,
            "tool_call": self.tool_call,
            "final_answer": self.final_answer,
            "http_status": self.http_status,
            "checked_at": self.checked_at,
        }


async def _system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(dial_host(host), port, type=0), _CONNECT_TIMEOUT_S
    )
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


def _extra_protected() -> tuple[ipaddress.IPv4Network, ...]:
    """The deployment's additions to the sandbox's protected space."""
    networks = []
    for item in re.split(r"[\s,]+", os.environ.get("AGENT_EGRESS_PROTECTED_CIDRS") or ""):
        if item:
            networks.append(ipaddress.IPv4Network(item, strict=False))
    return tuple(networks)


async def guard_base_url(
    base_url: str,
    *,
    resolve: Callable[[str, int], Awaitable[Iterable[str]]] | None = None,
) -> None:
    """Refuse a Base URL whose host a sandbox could not, or must not, reach.

    Every resolved address must pass the MCP connection policy (public only,
    never metadata or platform ranges) and, for IPv4, the sandbox's protected
    ranges; at least one must be IPv4. The dial that follows in this task is
    pinned to the checked IPv4 addresses only.
    """
    try:
        endpoint = validate_endpoint(base_url, EgressPolicy())
    except EndpointRejected:
        raise CheckStop("blocked_address") from None
    try:
        addresses = list(await (resolve or _system_resolve)(endpoint.host, endpoint.port))
    except (OSError, TimeoutError):
        raise CheckStop("unreachable") from None
    try:
        check_addresses(endpoint.host, addresses, EgressPolicy())
        extra = _extra_protected()
        ipv4 = [
            require_public_ipv4(address, extra)
            for address in addresses
            if ipaddress.ip_address(address).version == 4
        ]
    except (EndpointRejected, SshAccessError, ValueError):
        raise CheckStop("blocked_address") from None
    if not ipv4:
        raise CheckStop("blocked_address")
    # Pin the dial to the IPv4 addresses alone, which is all a sandbox reaches.
    check_addresses(endpoint.host, ipv4, EgressPolicy())


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport or pinned_transport(),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
        headers=IDENTITY_ENCODING_HEADERS,
    )


def _status_category(status: int) -> str | None:
    if 200 <= status < 300:
        return None
    if 300 <= status < 400:
        return "redirected"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status in (404, 405, 501):
        return "endpoint_missing"
    if status in (400, 413, 422):
        return "request_rejected"
    if status >= 500:
        return "upstream_error"
    return "unexpected_status"


async def _sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any] | None]:
    """Each JSON ``data:`` payload of a stream, ``None`` for ``[DONE]``; bounded."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        raise CheckStop("not_streaming", http_status=response.status_code)
    total = 0
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    async for chunk in iter_response_bytes(response):
        total += len(chunk)
        if total > _MAX_STREAM_BYTES:
            raise CheckStop("too_large", http_status=response.status_code)
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                yield None
                continue
            try:
                payload = json.loads(data)
            except ValueError:
                raise CheckStop("not_streaming", http_status=response.status_code) from None
            if isinstance(payload, dict):
                yield payload
    # A partial UTF-8 character at EOF is a broken stream too, even if an
    # earlier event had claimed completion.
    decoder.decode(b"", final=True)


@dataclass
class _Call:
    """One streamed tool call, assembled from its fragments."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _Turn:
    """What one streamed answer carried, and how it ended."""

    events: int = 0
    text: str = ""
    #: Tool calls by stream index (Chat) or in output order (Responses).
    calls: dict[int, _Call] = field(default_factory=dict)
    #: Responses output items exactly as the provider sent them, reasoning
    #: included, for the continuation to carry back.
    output: list[dict[str, Any]] = field(default_factory=list)
    #: The stream reached a successful end: a Chat ``finish_reason`` of
    #: ``stop``/``tool_calls``, or a Responses ``response.completed``.
    finished: bool = False
    #: The stream said it failed: a Chat ``error`` payload, or a Responses
    #: ``response.failed``/``response.incomplete``/``error`` event.
    failed: bool = False


_CHAT_FINISHED = frozenset({"stop", "tool_calls", "function_call"})
_RESPONSES_FAILED = frozenset({"response.failed", "response.incomplete", "error"})


def _event_items(payload: dict[str, Any], field_name: str) -> list[Any]:
    value = payload.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise CheckStop("invalid_response", http_status=200)
    return value


def _chat_event(turn: _Turn, payload: dict[str, Any]) -> None:
    if payload.get("error") is not None:
        turn.failed = True
        return
    for choice in _event_items(payload, "choices"):
        if not isinstance(choice, dict):
            continue
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason in _CHAT_FINISHED:
            turn.finished = True
        elif reason is not None:
            # ``length``, ``content_filter`` and the like: an answer cut short.
            turn.failed = True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if isinstance(delta.get("content"), str):
            turn.text += delta["content"]
        for position, call in enumerate(_event_items(delta, "tool_calls")):
            if not isinstance(call, dict):
                continue
            index = call.get("index", position)
            current = turn.calls.setdefault(index if isinstance(index, int) else -1, _Call())
            current.call_id = current.call_id or str(call.get("id") or "")
            function = call.get("function") or {}
            if isinstance(function, dict):
                current.name += str(function.get("name") or "")
                current.arguments += str(function.get("arguments") or "")


def _responses_event(turn: _Turn, payload: dict[str, Any]) -> None:
    kind = payload.get("type")
    if not isinstance(kind, str):
        raise CheckStop("invalid_response", http_status=200)
    if kind == "response.output_text.delta" and isinstance(payload.get("delta"), str):
        turn.text += payload["delta"]
    elif kind == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict):
            turn.output.append(item)
    elif kind == "response.completed":
        response = payload.get("response")
        status = response.get("status") if isinstance(response, dict) else None
        if status not in (None, "completed"):
            turn.failed = True
            return
        turn.finished = True
        output = response.get("output") if isinstance(response, dict) else None
        if isinstance(output, list) and output and all(isinstance(i, dict) for i in output):
            # The response's own record of its output is the authority.
            turn.output = list(output)
    elif kind in _RESPONSES_FAILED:
        turn.failed = True


def _responses_calls(turn: _Turn) -> None:
    for item in turn.output:
        if item.get("type") == "function_call":
            turn.calls[len(turn.calls)] = _Call(
                call_id=str(item.get("call_id") or ""),
                name=str(item.get("name") or ""),
                arguments=str(item.get("arguments") or ""),
            )


async def _stream(
    client: httpx.AsyncClient, url: str, api_key: str, body: dict[str, Any], protocol: str
) -> _Turn:
    """One streamed answer, refused unless it ended successfully."""
    turn = _Turn()
    headers = {"authorization": f"Bearer {api_key}", "accept": "text/event-stream"}
    try:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            category = _status_category(response.status_code)
            if category is not None:
                raise CheckStop(category, http_status=response.status_code)
            async for payload in _sse_events(response):
                turn.events += 1
                if payload is None:
                    continue
                if protocol == CHAT_COMPLETIONS:
                    _chat_event(turn, payload)
                else:
                    _responses_event(turn, payload)
    except httpx.ConnectError as exc:
        tls = "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper()
        raise CheckStop("tls" if tls else "unreachable") from None
    except (httpx.DecodingError, UnicodeDecodeError):
        raise CheckStop("invalid_response", http_status=200) from None
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, OSError):
        raise CheckStop("unreachable") from None
    if turn.events == 0:
        raise CheckStop("not_streaming", http_status=200)
    if turn.failed:
        raise CheckStop("stream_error", http_status=200)
    if not turn.finished:
        raise CheckStop("incomplete_stream", http_status=200)
    if protocol != CHAT_COMPLETIONS:
        _responses_calls(turn)
    return turn


def _the_call(turn: _Turn, nonce: str) -> _Call:
    """The one call to the offered tool, with a real id and the value asked for."""
    calls = [call for call in turn.calls.values() if call.name == _TOOL_NAME]
    if len(calls) != 1:
        raise CheckStop("no_tool_call")
    call = calls[0]
    if not call.call_id:
        raise CheckStop("missing_call_id")
    try:
        arguments = json.loads(call.arguments or "null")
    except ValueError:
        raise CheckStop("wrong_tool_arguments") from None
    if not isinstance(arguments, dict) or arguments.get("value") != nonce:
        raise CheckStop("wrong_tool_arguments")
    return call


def _requests(
    protocol: str, model: str, nonce: str
) -> tuple[str, dict[str, Any], Callable[[_Turn, _Call], dict[str, Any]]]:
    """The path, the first body, and how to build the second from the first answer."""
    ask = (
        f"This is a connectivity check. Call the {_TOOL_NAME} tool exactly once with "
        f'value "{nonce}". Do not answer in text.'
    )
    then = "Reply with the tool's result, verbatim, and nothing else."
    if protocol == CHAT_COMPLETIONS:
        tool = {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": _TOOL_DESCRIPTION,
                "parameters": _TOOL_PARAMETERS,
            },
        }
        first = {"model": model, "stream": True, "messages": [{"role": "user", "content": ask}]}
        first["tools"] = [tool]

        def second(turn: _Turn, call: _Call) -> dict[str, Any]:
            del turn
            return {
                "model": model,
                "stream": True,
                "tools": [tool],
                "messages": [
                    {"role": "user", "content": ask},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {"name": call.name, "arguments": call.arguments},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": call.call_id, "content": nonce},
                    {"role": "user", "content": then},
                ],
            }

        return "/chat/completions", first, second
    tool = {
        "type": "function",
        "name": _TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "parameters": _TOOL_PARAMETERS,
    }
    user = {"role": "user", "content": ask}
    first = {"model": model, "stream": True, "input": [user], "tools": [tool]}

    def second_responses(turn: _Turn, call: _Call) -> dict[str, Any]:
        # Every output item goes back as it came, reasoning included: a
        # reasoning model refuses a tool result whose reasoning context is gone.
        return {
            "model": model,
            "stream": True,
            "tools": [tool],
            "input": [
                user,
                *turn.output,
                {"type": "function_call_output", "call_id": call.call_id, "output": nonce},
                {"role": "user", "content": then},
            ],
        }

    return "/responses", first, second_responses


async def _run(
    result: CheckResult,
    base_url: str,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    nonce = f"check-{secrets.token_hex(4)}"
    path, first, second = _requests(result.protocol, result.model, nonce)
    async with _client(transport) as client:
        turn = await _stream(client, base_url + path, api_key, first, result.protocol)
        result.streaming = True
        call = _the_call(turn, nonce)
        result.tool_call = True
        final = await _stream(client, base_url + path, api_key, second(turn, call), result.protocol)
        if nonce not in final.text:
            raise CheckStop("no_final_answer")
        result.final_answer = True


async def check_connection(
    *,
    base_url: str,
    api_key: str,
    protocol: str,
    model: str,
    resolve: Callable[[str, int], Awaitable[Iterable[str]]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CheckResult:
    """Run one check and say what it found; never raises for an upstream's behaviour."""
    if protocol not in (CHAT_COMPLETIONS, RESPONSES) or not MODEL_ID.fullmatch(model):
        raise ValueError("unknown protocol or model id")
    result = CheckResult(protocol=protocol, model=model, category="supported")
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_S):
            await guard_base_url(base_url, resolve=resolve)
            await _run(result, base_url, api_key, transport=transport)
    except CheckStop as stop:
        result.category = stop.category
        result.http_status = stop.http_status
    except TimeoutError:
        result.category = "unreachable"
    return result


async def _bounded_get(
    *, client_transport: httpx.AsyncBaseTransport | None, url: str, key: str
) -> bytes:
    """One authenticated GET, read only up to the model-list bound."""
    chunks: list[bytes] = []
    total = 0
    async with _client(client_transport) as client:
        try:
            async with client.stream(
                "GET", url, headers={"authorization": f"Bearer {key}"}
            ) as response:
                category = _status_category(response.status_code)
                if category is not None:
                    raise CheckStop(category, http_status=response.status_code)
                async for chunk in iter_response_bytes(response):
                    total += len(chunk)
                    if total > _MAX_MODELS_BYTES:
                        raise CheckStop("too_large", http_status=response.status_code)
                    chunks.append(chunk)
        except httpx.DecodingError:
            raise CheckStop("invalid_response", http_status=200) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, OSError):
            raise CheckStop("unreachable") from None
    return b"".join(chunks)


async def discover_models(
    *,
    base_url: str,
    api_key: str,
    resolve: Callable[[str, int], Awaitable[Iterable[str]]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """The model ids ``GET /models`` lists, for the owner to pick from.

    A convenience only: many providers have no such endpoint, and a list says
    nothing about whether a model can run an agent. Raises ``CheckStop``.
    """
    async with asyncio.timeout(_CONNECT_TIMEOUT_S + _READ_TIMEOUT_S):
        await guard_base_url(base_url, resolve=resolve)
        body = await _bounded_get(client_transport=transport, url=base_url + "/models", key=api_key)
    try:
        listed = json.loads(body).get("data")
    except (ValueError, AttributeError):
        raise CheckStop("invalid_response", http_status=200) from None
    models: list[str] = []
    for entry in listed if isinstance(listed, list) else []:
        model = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(model, str) and MODEL_ID.fullmatch(model) and model not in models:
            models.append(model)
    return models[:_MAX_DISCOVERED]


__all__ = [
    "CHECKS_PER_HOUR",
    "CHECK_MESSAGES",
    "CheckResult",
    "CheckStop",
    "check_connection",
    "discover_models",
    "guard_base_url",
]
