"""What a connection check reads off the wire, whatever the protocol.

Shared by the check (``model_connection_check``) and each protocol's own
reader (``model_connection_anthropic`` for Messages): the stop a check ends
on, one streamed tool call, one streamed answer, and the bounded server-sent
event reader they all parse. Nothing here dials anything.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent.mcp.http_body import iter_response_bytes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

#: What one streamed answer may total before the check gives up on it.
MAX_STREAM_BYTES = 512 * 1024


class CheckStop(Exception):
    """The check ended early, for the category named."""

    def __init__(self, category: str, *, http_status: int | None = None) -> None:
        """Carry the category and, when the upstream answered, its status."""
        super().__init__(category)
        self.category = category
        self.http_status = http_status


@dataclass
class StreamedCall:
    """One streamed tool call, assembled from its fragments."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class StreamedTurn:
    """What one streamed answer carried, and how it ended."""

    events: int = 0
    text: str = ""
    #: Tool calls by stream index (Chat) or in output order (Responses, Messages).
    calls: dict[int, StreamedCall] = field(default_factory=dict)
    #: Responses output items exactly as the provider sent them, reasoning
    #: included, for the continuation to carry back.
    output: list[dict[str, Any]] = field(default_factory=list)
    #: The stream reached a successful end: a Chat ``finish_reason`` of
    #: ``stop``/``tool_calls``, a Responses ``response.completed``, or a
    #: Messages ``message_delta`` stop reason followed by ``message_stop``.
    finished: bool = False
    #: The stream said it failed: a Chat ``error`` payload, a Responses
    #: ``response.failed``/``response.incomplete``/``error`` event, or a
    #: Messages ``error`` event or a stop reason that is not a finished answer.
    failed: bool = False


def event_items(payload: dict[str, Any], field_name: str) -> list[Any]:
    """``payload[field_name]`` as a list, refusing anything else that is set."""
    value = payload.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise CheckStop("invalid_response", http_status=200)
    return value


async def sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any] | None]:
    """Each JSON ``data:`` payload of a stream, ``None`` for ``[DONE]``; bounded."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        raise CheckStop("not_streaming", http_status=response.status_code)
    total = 0
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    async for chunk in iter_response_bytes(response):
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
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


__all__ = [
    "MAX_STREAM_BYTES",
    "CheckStop",
    "StreamedCall",
    "StreamedTurn",
    "event_items",
    "sse_events",
]
