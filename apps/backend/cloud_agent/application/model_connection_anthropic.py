"""The Anthropic Messages half of a connection check, read natively.

Claude Code is the runtime that speaks Messages to a connection, so the check
asks exactly what the CLI will: ``POST /v1/messages?beta=true`` under the
connection's ``anthropic_base_url``, authenticated with ``x-api-key`` and
naming ``anthropic-version`` -- the headers the pinned CLI sends when it is
given ``ANTHROPIC_API_KEY``. Nothing is translated to or from another
protocol.

A Messages stream is framed: ``message_start``, then each content block's
``content_block_start``, deltas and ``content_block_stop``, then a
``message_delta`` carrying the stop reason and a final ``message_stop``. The
CLI treats a stream that ends anywhere short of that as a dropped connection,
and so does this reader. An ``error`` event, a delta for a block that was never
started or of a type that block cannot take, content after the
``message_delta``, a stop reason that is not a finished answer
(``max_tokens``, ``refusal``, ``pause_turn``) and a block left open all
refuse the answer.

The assistant's content goes back in the continuation exactly as it came,
signed thinking included: a model that thought before calling the tool
refuses a tool result whose thinking was dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from cloud_agent.application.model_connection_stream import (
    CheckStop,
    StreamedCall,
    StreamedTurn,
)
from cloud_agent_shared.model_connections import ANTHROPIC_VERSION, anthropic_url

if TYPE_CHECKING:
    from collections.abc import Callable

#: Room for a short tool call or answer after thinking, and no more.
_MAX_TOKENS = 1024
_FINISHED = frozenset({"end_turn", "tool_use", "stop_sequence"})
#: Each delta type, the block type it may extend, and the field it appends to.
#: A delta of any other type, or for another kind of block, is malformed.
_DELTAS: dict[str, tuple[str, str]] = {
    "text_delta": ("text", "text"),
    "input_json_delta": ("tool_use", "partial_json"),
    "thinking_delta": ("thinking", "thinking"),
    "signature_delta": ("thinking", "signature"),
}


@dataclass
class MessagesTurn(StreamedTurn):
    """A Messages answer: its content blocks in order, and its framing."""

    #: Every content block by index, as the continuation must carry it back.
    blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: The partial JSON of each tool_use block's input, by index.
    inputs: dict[int, str] = field(default_factory=dict)
    #: Blocks started and not yet stopped.
    open: set[int] = field(default_factory=set)
    started: bool = False
    #: A ``message_delta`` arrived; no content block may follow it.
    delta_seen: bool = False
    #: That ``message_delta`` named a finished answer's stop reason.
    stop_reason_ok: bool = False
    stopped: bool = False


def messages_url(base_url: str) -> str:
    """Where the CLI posts its requests for this connection's Base URL."""
    return anthropic_url(base_url, "/v1/messages") + "?beta=true"


def models_url(base_url: str) -> str:
    """Where a Messages provider lists its models."""
    return anthropic_url(base_url, "/v1/models")


def anthropic_headers(api_key: str) -> dict[str, str]:
    """The CLI's own authentication for ``ANTHROPIC_API_KEY``; never a bearer token."""
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}


def _invalid() -> CheckStop:
    return CheckStop("invalid_response", http_status=200)


def _index(payload: dict[str, Any]) -> int:
    index = payload.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise _invalid()
    return index


def _object(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("type", ""), str):
        raise _invalid()
    return value


def _block_start(turn: MessagesTurn, payload: dict[str, Any]) -> None:
    index = _index(payload)
    if index in turn.blocks or turn.delta_seen:
        raise _invalid()
    block = dict(_object(payload, "content_block"))
    turn.blocks[index] = block
    turn.open.add(index)
    if block.get("type") == "tool_use":
        turn.inputs[index] = ""
    elif block.get("type") == "text" and isinstance(block.get("text"), str):
        turn.text += block["text"]


def _append(block: dict[str, Any], name: str, value: str) -> None:
    block[name] = str(block.get(name) or "") + value


def _block_delta(turn: MessagesTurn, payload: dict[str, Any]) -> None:
    index = _index(payload)
    if index not in turn.open or turn.delta_seen:
        raise _invalid()
    delta = _object(payload, "delta")
    block = turn.blocks[index]
    block_type, name = _DELTAS.get(str(delta.get("type")), ("", ""))
    value = delta.get(name)
    if not block_type or block.get("type") != block_type or not isinstance(value, str):
        raise _invalid()
    if block_type == "tool_use":
        turn.inputs[index] += value
        return
    _append(block, name, value)
    if block_type == "text":
        turn.text += value


def _block_stop(turn: MessagesTurn, payload: dict[str, Any]) -> None:
    index = _index(payload)
    if index not in turn.open or turn.delta_seen:
        raise _invalid()
    turn.open.discard(index)


def _message_delta(turn: MessagesTurn, payload: dict[str, Any]) -> None:
    if turn.delta_seen:
        raise _invalid()
    turn.delta_seen = True
    reason = _object(payload, "delta").get("stop_reason")
    if reason in _FINISHED:
        turn.stop_reason_ok = True
    elif reason is not None:
        # Cut short or declined: never a finished answer.
        turn.failed = True


_BLOCK_EVENTS = {
    "content_block_start": _block_start,
    "content_block_delta": _block_delta,
    "content_block_stop": _block_stop,
    "message_delta": _message_delta,
}


def messages_event(streamed: StreamedTurn, payload: dict[str, Any]) -> None:
    """Fold one Messages event into the turn, refusing any out of frame."""
    turn = cast("MessagesTurn", streamed)
    kind = payload.get("type")
    if not isinstance(kind, str):
        raise _invalid()
    if kind == "error":
        turn.failed = True
    elif kind == "message_start":
        if turn.started:
            raise _invalid()
        _object(payload, "message")
        turn.started = True
    elif kind == "ping":
        return
    elif not turn.started or turn.stopped:
        raise _invalid()
    elif kind == "message_stop":
        turn.stopped = True
    elif kind in _BLOCK_EVENTS:
        _BLOCK_EVENTS[kind](turn, payload)


def _tool_input(arguments: str, block: dict[str, Any]) -> tuple[str, Any]:
    """The call's arguments as streamed, and the input object the continuation carries."""
    if not arguments:
        start = block.get("input")
        return json.dumps(start if start is not None else {}), start or {}
    try:
        return arguments, json.loads(arguments)
    except ValueError:
        # Left for the call check to refuse as the wrong arguments.
        return arguments, {}


def settle_messages(streamed: StreamedTurn) -> None:
    """Whether the frame closed, and the tool calls the content blocks carry."""
    turn = cast("MessagesTurn", streamed)
    if turn.open:
        raise CheckStop("incomplete_stream", http_status=200)
    turn.finished = turn.started and turn.stop_reason_ok and turn.stopped
    for index in sorted(turn.blocks):
        block = turn.blocks[index]
        if block.get("type") != "tool_use":
            continue
        arguments, parsed = _tool_input(turn.inputs.get(index, ""), block)
        block["input"] = parsed
        turn.calls[len(turn.calls)] = StreamedCall(
            call_id=str(block.get("id") or ""),
            name=str(block.get("name") or ""),
            arguments=arguments,
        )


def _content(turn: MessagesTurn) -> list[dict[str, Any]]:
    """The assistant's blocks in order; an empty text block is not content."""
    blocks = [turn.blocks[index] for index in sorted(turn.blocks)]
    return [b for b in blocks if not (b.get("type") == "text" and not b.get("text"))]


def messages_requests(
    model: str, nonce: str, *, ask: str, then: str, tool: dict[str, Any]
) -> tuple[dict[str, Any], Callable[[StreamedTurn, StreamedCall], dict[str, Any]]]:
    """The first request, and how to build the second from the first answer."""
    messages_tool = {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool["parameters"],
    }
    user = {"role": "user", "content": ask}
    first = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "stream": True,
        "tools": [messages_tool],
        "messages": [user],
    }

    def second(turn: StreamedTurn, call: StreamedCall) -> dict[str, Any]:
        content = _content(cast("MessagesTurn", turn))
        return {
            **first,
            "messages": [
                user,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call.call_id, "content": nonce},
                        {"type": "text", "text": then},
                    ],
                },
            ],
        }

    return first, second


__all__ = [
    "MessagesTurn",
    "anthropic_headers",
    "messages_event",
    "messages_requests",
    "messages_url",
    "models_url",
    "settle_messages",
]
