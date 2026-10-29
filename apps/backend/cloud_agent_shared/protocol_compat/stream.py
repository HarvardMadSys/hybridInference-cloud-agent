"""A Chat Completions answer, as the client protocol's answer -- or as its failure.

The vendored stream translators close an answer on whatever the upstream
stopped with: given a stream that simply ends, the Responses translator emits
``response.completed`` and the Messages one ``end_turn``. A provider that
dropped the connection mid-answer would then look to the CLI like a model that
had finished. ``ClientStream`` checks every payload before a translator sees
it and decides the ending itself:

- choice 0 reached a ``finish_reason`` Chat Completions defines, and nothing
  went wrong before the stream ended: the translator's own close
  (``completed``, ``incomplete`` for ``length``, ``tool_use`` ...);
- anything else -- an ``error`` payload, a malformed payload or choice, a
  choice other than 0, an unknown finish reason, a stream that broke off or
  ended without finishing (``fail``), even after a finish reason -- a failure
  in the client's own vocabulary: Responses ``response.failed`` and ``error``,
  Messages ``error``. Never a success.

Nothing the upstream said is copied into a failure: its text may echo the key
or anything else, so a failure names only what happened.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import ANTHROPIC_MESSAGES, RESPONSES
from .messages_chat import OpenAIToAnthropicStreamTranslator, openai_response_to_anthropic
from .namespaces import restore_payload
from .responses_chat import (
    ResponsesStreamTranslator,
    chat_response_to_responses,
    new_response_id,
    now_ts,
)

if TYPE_CHECKING:
    from .namespaces import ToolNames

#: The ends of an answer Chat Completions defines; anything else is not one.
FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter", "function_call"})

_INCOMPLETE = "The model provider's stream ended before the answer finished."
_UPSTREAM_ERROR = "The model provider reported an error inside the stream."
_MALFORMED = "The model provider's stream could not be read."
#: What a vendored translator raises on a provider shape it did not expect
#: (a nested ``tool_calls`` that is not a list of objects, say): the answer is
#: then unreadable, reported as such, never a traceback.
_TRANSLATOR_ERRORS = (AttributeError, TypeError, KeyError, IndexError, ValueError)


def _data_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _messages_error(message: str) -> str:
    payload = {"type": "error", "error": {"type": "api_error", "message": message}}
    return f"event: error\ndata: {json.dumps(payload)}\n\n"


def _text_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _valid_tool_calls(calls: Any) -> bool:
    """``tool_calls`` as Chat Completions defines it: a list of call objects."""
    if calls is None:
        return True
    if not isinstance(calls, list):
        return False
    for call in calls:
        if not isinstance(call, dict) or not _text_or_none(call.get("id")):
            return False
        function = call.get("function")
        if function is None:
            continue
        if not isinstance(function, dict):
            return False
        if not (_text_or_none(function.get("name")) and _text_or_none(function.get("arguments"))):
            return False
    return True


def _valid_message(message: Any) -> bool:
    """A streamed ``delta`` or a whole ``message``: text where text belongs, calls as calls."""
    if message is None:
        return True
    if not isinstance(message, dict):
        return False
    texts = ("content", "reasoning_content", "reasoning", "thinking", "role")
    return all(_text_or_none(message.get(field)) for field in texts) and _valid_tool_calls(
        message.get("tool_calls")
    )


def _finish_of(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether the payload is well-formed for choice 0, and the finish reason it carries."""
    choices = payload.get("choices", [])
    if not isinstance(choices, list):
        return False, None
    finish: str | None = None
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            return False, None
        if not _valid_message(choice.get("delta")):
            return False, None
        reason = choice.get("finish_reason")
        if reason is not None and not (isinstance(reason, str) and reason in FINISH_REASONS):
            return False, None
        finish = reason or finish
    return True, finish


class ClientStream:
    """Feed Chat Completions stream payloads; read the client's SSE frames."""

    def __init__(
        self,
        client: str,
        *,
        model: str,
        request_body: dict[str, Any],
        names: ToolNames | None = None,
    ) -> None:
        """Start one answer for ``client`` on ``model``, echoing ``request_body`` where due."""
        if client not in (RESPONSES, ANTHROPIC_MESSAGES):
            raise ValueError(f"unknown client protocol {client!r}")
        self.client = client
        self._names = names
        self._finished = False
        self._failure: str | None = None
        self._closed = False
        self._responses = (
            ResponsesStreamTranslator(
                response_id=new_response_id(),
                created_at=now_ts(),
                model=model,
                request_body=request_body,
                store=False,
            )
            if client == RESPONSES
            else None
        )
        self._messages = (
            OpenAIToAnthropicStreamTranslator(model=model) if client == ANTHROPIC_MESSAGES else None
        )

    @property
    def succeeded(self) -> bool:
        """Whether choice 0 finished and nothing failed."""
        return self._finished and self._failure is None

    def fail(self, message: str = _INCOMPLETE) -> None:
        """Record that the stream broke off or could not be read; the answer will fail."""
        self._failure = self._failure or message

    def feed(self, payload: dict[str, Any] | None) -> list[str]:
        """Translate one upstream payload (``None`` is ``[DONE]``) into client frames."""
        if payload is None or self._closed or self._failure is not None:
            return []
        if payload.get("error") is not None:
            self.fail(_UPSTREAM_ERROR)
            return []
        valid, finish = _finish_of(payload)
        if not valid:
            self.fail(_MALFORMED)
            return []
        try:
            frames = self._translate(payload)
        except _TRANSLATOR_ERRORS:
            self.fail(_MALFORMED)
            return []
        self._finished = self._finished or finish is not None
        return frames

    def _translate(self, payload: dict[str, Any]) -> list[str]:
        if self._responses is not None:
            return self._restore(list(self._responses.feed(_data_frame(payload))))
        if self._messages is None:
            return []
        return [frame.decode() for frame in self._messages.feed(_data_frame(payload).encode())]

    def finish(self) -> list[str]:
        """The closing frames: the translator's own close, or a failure."""
        if self._closed:
            return []
        self._closed = True
        if not self._finished:
            self.fail(_INCOMPLETE)
        if self._responses is not None:
            if self._failure is not None:
                error = {"error": {"code": "upstream_error", "message": self._failure}}
                list(self._responses.feed(_data_frame(error)))
            return self._restore(list(self._responses.finalize()))
        if self._messages is None:
            return []
        if self._failure is not None:
            return [_messages_error(self._failure)]
        return [frame.decode() for frame in self._messages.finalize()]

    def _restore(self, frames: list[str]) -> list[str]:
        """Responses frames with namespaced function calls under their own names."""
        if not self._names:
            return frames
        return [_restore_frame(frame, self._names) for frame in frames]


def _restore_frame(frame: str, names: ToolNames) -> str:
    lines = []
    for line in frame.split("\n"):
        if line.startswith("data: ") and line[6:].strip().startswith("{"):
            line = "data: " + json.dumps(restore_payload(json.loads(line[6:]), names))
        lines.append(line)
    return "\n".join(lines)


def client_response(
    client: str,
    chat: dict[str, Any],
    *,
    model: str,
    request_body: dict[str, Any],
    names: ToolNames | None = None,
) -> dict[str, Any] | None:
    """A non-streamed Chat Completions answer as the client's, or ``None`` if unfinished."""
    choices = chat.get("choices") if isinstance(chat, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    if choices[0].get("index", 0) != 0 or not (
        isinstance(reason, str) and reason in FINISH_REASONS
    ):
        return None
    if not _valid_message(choices[0].get("message")):
        return None
    try:
        return _whole_answer(client, chat, model=model, request_body=request_body, names=names)
    except _TRANSLATOR_ERRORS:
        return None


def _whole_answer(
    client: str,
    chat: dict[str, Any],
    *,
    model: str,
    request_body: dict[str, Any],
    names: ToolNames | None,
) -> dict[str, Any]:
    if client == RESPONSES:
        response = chat_response_to_responses(
            chat,
            response_id=new_response_id(),
            created_at=now_ts(),
            model=model,
            request_body=request_body,
            store=False,
        )
        return restore_payload(response, names) if names else response
    return openai_response_to_anthropic(chat, model=model)


_MESSAGES_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


def client_error(client: str, status: int, message: str) -> dict[str, Any]:
    """An error body in ``client``'s own shape; ``message`` must be safe to show."""
    if client == ANTHROPIC_MESSAGES:
        kind = _MESSAGES_ERROR_TYPES.get(status, "api_error")
        return {"type": "error", "error": {"type": kind, "message": message}}
    kind = "invalid_request_error" if 400 <= status < 500 else "server_error"
    return {"error": {"type": kind, "code": None, "param": None, "message": message}}


def parse_frames(frames: list[str]) -> list[dict[str, Any]]:
    """The JSON payloads of SSE frames, in order: what a client parser reads."""
    payloads: list[dict[str, Any]] = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    payloads.append(json.loads(data))
    return payloads


__all__ = [
    "FINISH_REASONS",
    "ClientStream",
    "client_error",
    "client_response",
    "parse_frames",
]
