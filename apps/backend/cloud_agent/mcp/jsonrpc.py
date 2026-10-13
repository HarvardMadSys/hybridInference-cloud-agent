"""JSON-RPC 2.0 framing and correlation for MCP.

The old relay filtered a response by its *shape* -- anything with ``result.tools``
was treated as a catalogue. That worked while every reply arrived on the POST
that asked for it. It stops working the moment a server answers on the
long-lived GET stream, sends a progress notification first, or asks the client
a question of its own, which is why this module exists: a request is tracked by
its id, and everything else is routed by what it is.

Three details are easy to get wrong and are handled here once:

**Ids are not integers.** The specification allows a string or a number, and a
server may answer ``"1"`` to a request sent with ``1``. Ids are normalized to
their string form for correlation, and the original is echoed back verbatim on
anything we send.

**A batch is answered as a batch.** Every element is routed independently, and
only elements that are requests get replies; a batch of notifications produces
no response at all rather than an empty array.

**A server's own request is not a response.** ``sampling/createMessage`` and
``elicitation/create`` arrive with an id and expect one back. Treating them as
replies to something we sent is how a correlation table silently loses entries.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: JSON-RPC's own codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: MCP's own codes.
RESOURCE_NOT_FOUND = -32002
#: A metadata header did not match the body it mirrors. Stateless era only.
HEADER_MISMATCH = -32020
#: The request needed a client capability that was not declared.
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
#: The per-request protocol version is not one the server implements.
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: Every code the specification reserves for MCP itself. A reply carrying one
#: of these is unambiguously a modern-era server's answer, which is what era
#: detection keys on.
MCP_RESERVED_CODES: frozenset[int] = frozenset(range(-32099, -32019))

#: A single frame, request or response, is bounded before it is parsed. Four
#: megabytes is far above any real catalogue and far below what would let one
#: connection exhaust a shared control plane.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class JsonRpcError(Exception):
    """A JSON-RPC error reply, carrying the code the peer expects."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        """Build the error from its protocol fields."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_dict(self, request_id: Any) -> dict[str, Any]:
        """Render the error as a JSON-RPC response body."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


class UnsupportedProtocolVersion(JsonRpcError):
    """The server named the versions it does implement, so we can retry."""

    def __init__(self, message: str, supported: list[str], requested: str = "") -> None:
        """Record the server's advertised list alongside the error."""
        super().__init__(UNSUPPORTED_PROTOCOL_VERSION, message, {"supported": list(supported)})
        self.supported = list(supported)
        self.requested = requested


class ProtocolViolation(JsonRpcError):
    """A payload that is not JSON-RPC at all, as opposed to a failed call."""

    def __init__(self, message: str) -> None:
        """Build the violation with the standard invalid-request code."""
        super().__init__(INVALID_REQUEST, message)


def id_key(value: Any) -> str:
    """Normalize a request id for correlation.

    ``None`` is the notification marker and is never a key; anything else keeps
    its text form so that ``1`` and ``"1"`` correlate with the same pending
    entry, which is what the specification's "SHOULD NOT be null" leaves us.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ProtocolViolation("a request id cannot be a boolean")
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    raise ProtocolViolation("a request id must be a string or a number")


@dataclass(frozen=True)
class Incoming:
    """One decoded JSON-RPC message, classified but not yet routed."""

    payload: Mapping[str, Any]

    @property
    def raw_id(self) -> Any:
        """The id exactly as it arrived."""
        return self.payload.get("id")

    @property
    def key(self) -> str:
        """The correlation key, or ``""`` for a notification."""
        return id_key(self.payload.get("id")) if "id" in self.payload else ""

    @property
    def method(self) -> str | None:
        """The method name when this is a request or a notification."""
        value = self.payload.get("method")
        return value if isinstance(value, str) else None

    @property
    def params(self) -> Mapping[str, Any]:
        """Request parameters, always a mapping."""
        value = self.payload.get("params")
        return value if isinstance(value, Mapping) else {}

    @property
    def is_response(self) -> bool:
        """Whether this is a reply to something we sent."""
        return "id" in self.payload and ("result" in self.payload or "error" in self.payload)

    @property
    def is_notification(self) -> bool:
        """Whether this is a notification: a method with no id."""
        return "id" not in self.payload and self.method is not None

    @property
    def is_request(self) -> bool:
        """Whether this is a request from the peer that expects a reply."""
        return "id" in self.payload and self.method is not None

    @property
    def is_error(self) -> bool:
        """Whether a reply carries an error."""
        return self.is_response and "error" in self.payload

    def result(self) -> Any:
        """The reply's result, raising if it carried an error instead."""
        if self.is_error:
            raise self.error()
        return self.payload.get("result")

    def error(self) -> JsonRpcError:
        """Build the exception this reply's error describes.

        The unsupported-version case is separated because it is the one error a
        caller can act on: it carries the versions the server does implement,
        and a retry with one of them is a supported operation rather than a
        guess.

        A reply whose ``error`` is not an object, or whose code is not a number,
        still says the call failed, so it is read as an internal error rather
        than raised as whatever building the exception tripped over: that
        exception escapes every caller's handling of a failed call.
        """
        error = self.payload.get("error")
        if not isinstance(error, Mapping):
            error = {}
        code = _error_code(error.get("code", INTERNAL_ERROR))
        message = str(error.get("message", "the server reported an error"))
        data = error.get("data")
        if code == UNSUPPORTED_PROTOCOL_VERSION:
            supported = data.get("supported") if isinstance(data, Mapping) else None
            requested = data.get("requested") if isinstance(data, Mapping) else None
            return UnsupportedProtocolVersion(
                message,
                [str(item) for item in supported] if isinstance(supported, list) else [],
                str(requested or ""),
            )
        return JsonRpcError(code, message, data)


def _error_code(value: Any) -> int:
    """An error's code as an integer, or the internal-error code when it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return INTERNAL_ERROR


def decode(body: bytes | str) -> list[Incoming]:
    """Decode one frame into messages, refusing anything malformed.

    An empty body is a valid empty batch in neither direction, so it is a
    violation rather than an empty list: silently accepting it would turn a
    truncated stream into "the server had nothing to say".

    Every refusal is a :class:`JsonRpcError`, including bytes that are not
    UTF-8 and nesting deeper than the parser recurses: a peer's malformed frame
    is a parse error, and a built-in exception in its place skips the handling
    every caller has for one.
    """
    try:
        text = body.decode() if isinstance(body, bytes) else body
    except UnicodeDecodeError as exc:
        raise JsonRpcError(PARSE_ERROR, "the payload is not UTF-8 text") from exc
    if not text.strip():
        raise ProtocolViolation("an empty payload is not a JSON-RPC message")
    if len(text.encode()) > MAX_MESSAGE_BYTES:
        raise ProtocolViolation("this message is larger than the protocol allows")
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise JsonRpcError(PARSE_ERROR, f"invalid JSON: {exc}") from exc
    except RecursionError as exc:
        raise JsonRpcError(
            PARSE_ERROR, "the payload nests deeper than this platform parses"
        ) from exc
    items = document if isinstance(document, list) else [document]
    if not items:
        raise ProtocolViolation("an empty batch is not a JSON-RPC message")
    incoming: list[Incoming] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ProtocolViolation("every JSON-RPC message must be an object")
        if item.get("jsonrpc") not in (None, "2.0"):
            raise ProtocolViolation("only JSON-RPC 2.0 is supported")
        incoming.append(Incoming(payload=item))
    return incoming


def encode_request(request_id: Any, method: str, params: Mapping[str, Any] | None = None) -> bytes:
    """Serialize one request."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = dict(params)
    return json.dumps(payload, separators=(",", ":")).encode()


def encode_notification(method: str, params: Mapping[str, Any] | None = None) -> bytes:
    """Serialize one notification."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = dict(params)
    return json.dumps(payload, separators=(",", ":")).encode()


def encode_result(request_id: Any, result: Any) -> bytes:
    """Serialize a successful reply."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": result}, separators=(",", ":")
    ).encode()


def encode_error(request_id: Any, error: JsonRpcError | Mapping[str, Any]) -> bytes:
    """Serialize an error reply."""
    body = error.as_dict(request_id) if isinstance(error, JsonRpcError) else dict(error)
    return json.dumps(body, separators=(",", ":")).encode()


NotificationHandler = Callable[[Incoming], None]
RequestHandler = Callable[[Incoming], Any]


@dataclass
class Correlator:
    """Routes decoded messages to the waiter, handler or slot that owns them.

    Deliberately synchronous: it holds no lock and awaits nothing, so it can be
    driven from whichever task happens to have the bytes -- the POST that sent a
    request, or the GET stream that is receiving everyone's replies. Anything
    that needs to await belongs to the caller, which keeps the routing table
    impossible to deadlock.
    """

    notifications: dict[str, NotificationHandler] = field(default_factory=dict)
    requests: dict[str, RequestHandler] = field(default_factory=dict)
    _pending: dict[str, _Slot] = field(default_factory=dict, init=False)
    _unmatched: list[Incoming] = field(default_factory=list, init=False)

    def open(self, key: str) -> _Slot:
        """Register interest in the reply to ``key``."""
        slot = _Slot(key=key)
        self._pending[key] = slot
        return slot

    def close(self, key: str) -> None:
        """Forget a pending request, whether or not it was answered."""
        self._pending.pop(key, None)

    def pending_keys(self) -> list[str]:
        """Ids still awaiting a reply."""
        return sorted(self._pending)

    def unmatched(self) -> list[Incoming]:
        """Replies that arrived with no waiter, in arrival order.

        Kept rather than dropped: a reply can legitimately arrive after its
        waiter gave up, and a caller draining a stream wants to see that it
        happened instead of silently discarding a tool result.
        """
        return list(self._unmatched)

    def route(self, message: Incoming) -> list[Incoming]:
        """Deliver one message, returning any peer requests needing a reply."""
        if message.is_response:
            slot = self._pending.pop(message.key, None)
            if slot is None:
                self._unmatched.append(message)
            else:
                slot.deliver(message)
            return []
        if message.is_notification:
            handler = self.notifications.get(message.method or "")
            if handler is not None:
                handler(message)
            return []
        if message.is_request:
            handler = self.requests.get(message.method or "")
            if handler is None:
                return [message]
            result = handler(message)
            slot = self._pending.get(message.key)
            if slot is not None:
                slot.deliver_result(result)
            return []
        raise ProtocolViolation("a message must be a request, a notification or a response")

    def route_all(self, messages: Iterable[Incoming]) -> list[Incoming]:
        """Route a batch, returning every peer request that needs an answer."""
        unanswered: list[Incoming] = []
        for message in messages:
            unanswered.extend(self.route(message))
        return unanswered


@dataclass
class _Slot:
    """One waiter's mailbox, filled either by a reply or by a local handler."""

    key: str
    messages: list[Incoming] = field(default_factory=list, init=False)
    resolved: bool = field(default=False, init=False)

    def deliver(self, message: Incoming) -> None:
        """Record a reply that arrived from the peer."""
        self.messages.append(message)
        self.resolved = True

    def deliver_result(self, result: Any) -> None:
        """Record a result produced locally for a request we forwarded."""
        self.messages.append(Incoming(payload={"jsonrpc": "2.0", "id": self.key, "result": result}))
        self.resolved = True

    @property
    def answered(self) -> bool:
        """Whether anything has been delivered."""
        return self.resolved


__all__ = [
    "HEADER_MISMATCH",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "MAX_MESSAGE_BYTES",
    "MCP_RESERVED_CODES",
    "METHOD_NOT_FOUND",
    "MISSING_REQUIRED_CLIENT_CAPABILITY",
    "PARSE_ERROR",
    "RESOURCE_NOT_FOUND",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "Correlator",
    "Incoming",
    "JsonRpcError",
    "ProtocolViolation",
    "UnsupportedProtocolVersion",
    "decode",
    "encode_error",
    "encode_notification",
    "encode_request",
    "encode_result",
    "id_key",
]
