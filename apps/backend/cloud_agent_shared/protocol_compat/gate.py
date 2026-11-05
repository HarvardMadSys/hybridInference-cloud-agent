"""What a translated request may carry, and the Chat Completions request it becomes.

The vendored kernels translate what they recognize and skip the rest: a hosted
tool, an ``input_file`` part or a ``document`` block would vanish from the
request while the CLI believed it had been sent. That is not compatibility, so
every request passes this gate first. It refuses -- with a client-shaped 400
naming what is unsupported -- anything a Chat Completions upstream cannot
carry, and admits only the stateless subset: the whole conversation replayed
on every request, function tools (namespaced ones flattened and restored by
``namespaces``), text and images -- a tool's images included, carried after
its tool message (``tool_images``) since a tool message holds text only.

Three kinds of field are knowingly not forwarded, and documented as such:

- prior reasoning (Responses ``reasoning`` items, Messages ``thinking`` and
  ``redacted_thinking`` blocks): encrypted or signed for the provider that
  produced it, with no Chat Completions field to carry it;
- generation hints with no Chat equivalent (``thinking``,
  ``context_management``, ``output_config``, ``reasoning``, ``include``) --
  an owner connection runs at effort Auto;
- client bookkeeping (``metadata``, ``client_metadata``, ``prompt_cache_key``,
  ``store``), which identifies the client device and is not the provider's.

The model is locked: a request naming another model is refused, never
rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import ANTHROPIC_MESSAGES, RESPONSES
from .messages_chat import anthropic_request_to_openai
from .namespaces import NameCollision, ToolNames, flatten_request
from .responses_chat import (
    merge_leading_system_messages,
    responses_input_to_messages,
    responses_request_to_chat_params,
)
from .tool_images import move_tool_images

_RESPONSES_STATEFUL = ("previous_response_id", "conversation", "prompt")
_RESPONSES_ITEMS = frozenset(
    {None, "message", "function_call", "function_call_output", "reasoning"}
)
_RESPONSES_PARTS = frozenset({"input_text", "output_text", "text", "summary_text", "input_image"})
_TEXT_FORMATS = frozenset({"text", "json_object", "json_schema"})
_MESSAGES_BLOCKS = frozenset(
    {"text", "image", "tool_use", "tool_result", "thinking", "redacted_thinking"}
)
_TOOL_RESULT_BLOCKS = frozenset({"text", "image"})
_IMAGE_SOURCES = frozenset({"base64", "url"})
_MESSAGES_UNSUPPORTED_FIELDS = ("mcp_servers", "container")


class CompatRejected(Exception):
    """A request this layer will not translate; the message is safe to show."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        """Carry the client-facing reason and the HTTP status to answer with."""
        super().__init__(message)
        self.message = message
        self.status = status


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompatRejected(message)


def _check_model(body: dict[str, Any], model: str) -> None:
    requested = body.get("model")
    _require(
        requested in (None, model),
        f"This compatibility endpoint serves only the model {model!r}.",
    )


def _type_name(value: Any) -> str:
    kind = value.get("type") if isinstance(value, dict) else None
    return str(kind) if kind is not None else type(value).__name__


# -- Responses ----------------------------------------------------------------


def _check_responses_tools(body: dict[str, Any]) -> None:
    tools = body.get("tools")
    if tools is None:
        return
    _require(isinstance(tools, list), "tools must be a list.")
    unsupported = sorted(
        {_type_name(t) for t in tools if not isinstance(t, dict) or t.get("type") != "function"}
    )
    _require(
        not unsupported,
        "A Chat Completions upstream runs function tools only; unsupported: "
        + ", ".join(unsupported),
    )
    choice = body.get("tool_choice")
    _require(
        choice is None
        or isinstance(choice, str)
        or (isinstance(choice, dict) and choice.get("type") == "function"),
        "Only auto, none, required or a function tool_choice can be translated.",
    )


def _check_responses_parts(content: Any) -> None:
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, str):
            continue
        kind = part.get("type") if isinstance(part, dict) else None
        _require(
            kind in _RESPONSES_PARTS,
            f"Content part {_type_name(part)!r} cannot be sent to a Chat Completions upstream.",
        )
        _require(
            kind != "input_image" or bool(part.get("image_url")),
            "Only images given by URL or data URL can be translated.",
        )


def _check_responses_input(value: Any) -> None:
    if value is None or isinstance(value, str):
        return
    _require(isinstance(value, list), "input must be a string or a list of items.")
    for item in value:
        if isinstance(item, str):
            continue
        _require(isinstance(item, dict), "Every input item must be an object.")
        _require(
            item.get("type") in _RESPONSES_ITEMS,
            f"Input item {_type_name(item)!r} cannot be sent to a Chat Completions upstream.",
        )
        if item.get("type") in (None, "message"):
            _check_responses_parts(item.get("content"))
        if item.get("type") == "function_call_output":
            _check_responses_parts(item.get("output"))


def check_responses(body: dict[str, Any], model: str) -> None:
    """Refuse a Responses request a Chat Completions upstream cannot honor."""
    _check_model(body, model)
    for field in _RESPONSES_STATEFUL:
        _require(
            body.get(field) is None,
            f"{field} needs server-side state; this endpoint replays the full input instead.",
        )
    _require(not body.get("background"), "Background responses are not supported.")
    _check_responses_tools(body)
    _check_responses_input(body.get("input"))
    text = body.get("text")
    fmt = text.get("format") if isinstance(text, dict) else None
    _require(
        not isinstance(fmt, dict) or fmt.get("type") in _TEXT_FORMATS,
        "Only text, json_object and json_schema output formats can be translated.",
    )


def responses_chat_request(body: dict[str, Any], model: str) -> Prepared:
    """The Chat Completions request for a gated Responses request, and its tool names.

    Namespaced function tools, and history calls to them, are flattened first;
    anything else inside a namespace is then refused like any other tool.
    """
    try:
        body, names = flatten_request(body)
    except NameCollision:
        raise CompatRejected(
            "Two tools would share one name on a Chat Completions upstream."
        ) from None
    check_responses(body, model)
    instructions = body.get("instructions")
    messages = responses_input_to_messages(
        body.get("input"), instructions=instructions if isinstance(instructions, str) else None
    )
    params, _dropped = responses_request_to_chat_params(body)
    params.pop("reasoning_effort", None)
    if isinstance(body.get("parallel_tool_calls"), bool) and params.get("tools"):
        params["parallel_tool_calls"] = body["parallel_tool_calls"]
    chat = _chat_body(model, move_tool_images(merge_leading_system_messages(messages)), params)
    return Prepared(chat=chat, names=names if names else None)


# -- Anthropic Messages -------------------------------------------------------


def _check_image(block: dict[str, Any]) -> None:
    source = block.get("source")
    _require(
        isinstance(source, dict) and source.get("type") in _IMAGE_SOURCES,
        "Only base64 or URL images can be translated.",
    )


def _check_tool_result(block: dict[str, Any]) -> None:
    content = block.get("content")
    if not isinstance(content, list):
        return
    for inner in content:
        _require(
            isinstance(inner, dict) and inner.get("type") in _TOOL_RESULT_BLOCKS,
            f"Tool result block {_type_name(inner)!r} cannot be sent to a Chat Completions "
            "upstream.",
        )
        if inner.get("type") == "image":
            _check_image(inner)


def _check_message_blocks(content: Any) -> None:
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, str):
            continue
        kind = block.get("type") if isinstance(block, dict) else None
        _require(
            kind in _MESSAGES_BLOCKS,
            f"Content block {_type_name(block)!r} cannot be sent to a Chat Completions upstream.",
        )
        if kind == "image":
            _check_image(block)
        elif kind == "tool_result":
            _check_tool_result(block)


def _check_messages_tools(body: dict[str, Any]) -> None:
    tools = body.get("tools") or []
    _require(isinstance(tools, list), "tools must be a list.")
    server = sorted(
        {
            _type_name(t)
            for t in tools
            if not isinstance(t, dict) or t.get("type") not in (None, "custom") or not t.get("name")
        }
    )
    _require(
        not server,
        "A Chat Completions upstream runs client tools only; unsupported: " + ", ".join(server),
    )


def _check_system(system: Any) -> None:
    if system is None or isinstance(system, str):
        return
    _require(isinstance(system, list), "system must be a string or a list of text blocks.")
    for block in system:
        _require(
            isinstance(block, dict) and block.get("type") == "text",
            "Only text system blocks can be translated.",
        )


def check_messages(body: dict[str, Any], model: str) -> None:
    """Refuse a Messages request a Chat Completions upstream cannot honor."""
    _check_model(body, model)
    for field in _MESSAGES_UNSUPPORTED_FIELDS:
        _require(not body.get(field), f"{field} is not supported on a Chat Completions upstream.")
    _check_system(body.get("system"))
    _check_messages_tools(body)
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise CompatRejected("messages must be a list.")
    for message in messages:
        _require(isinstance(message, dict), "Every message must be an object.")
        if message.get("role") == "system":
            _check_system(message.get("content"))
        else:
            _check_message_blocks(message.get("content"))


def messages_chat_request(body: dict[str, Any], model: str) -> Prepared:
    """The Chat Completions request for a gated Anthropic Messages request."""
    check_messages(body, model)
    messages, params = anthropic_request_to_openai(body)
    # ``user`` would carry the client's device identifier to the provider.
    params.pop("user", None)
    params.pop("reasoning_effort", None)
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "none":
        params["tool_choice"] = "none"
    return Prepared(chat=_chat_body(model, move_tool_images(messages), params))


# -- shared -------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """A gated request as Chat Completions, with what restoring its answer needs."""

    chat: dict[str, Any]
    #: The flat names of namespaced Responses tools; ``None`` when there are none.
    names: ToolNames | None = None


def _chat_body(
    model: str, messages: list[dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    _require(bool(messages), "The request has no messages to send.")
    body = {**params, "model": model, "messages": messages}
    if body.get("stream"):
        body["stream"] = True
        # Usage arrives in a final chunk only when asked for.
        body["stream_options"] = {"include_usage": True}
    else:
        body.pop("stream", None)
    return body


def prepare(client: str, body: Any, model: str) -> Prepared:
    """Gate ``body`` from ``client`` and build its Chat Completions request."""
    _require(isinstance(body, dict), "The request body must be a JSON object.")
    if client == RESPONSES:
        return responses_chat_request(body, model)
    if client == ANTHROPIC_MESSAGES:
        return messages_chat_request(body, model)
    raise ValueError(f"unknown client protocol {client!r}")


__all__ = [
    "CompatRejected",
    "Prepared",
    "check_messages",
    "check_responses",
    "prepare",
]
