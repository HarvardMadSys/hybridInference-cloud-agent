r"""Server-sent events, parsed the way the transport specification requires.

Two MCP transports use SSE and they frame it differently enough that one
parser has to know both. The legacy 2024-11-05 transport keeps one long stream
open for the server and posts requests to a second endpoint; Streamable HTTP
answers a POST with either a JSON body or a short SSE stream, and may also hold
a GET stream open. In both cases the client sees the same wire format, so the
framing lives here and the difference stays in the transport.

The rules that a naive ``split(b"\\n\\n")`` gets wrong:

- A line ends with LF, CRLF or a bare CR. Servers in the wild send all three,
  and treating CRLF as part of the data corrupts every JSON body in the stream.
- Fields whose name the client does not recognise are ignored, not treated as
  errors. That is what makes the format extensible.
- A field with no colon is a name with an empty value, not a malformed line.
- One leading space after the colon is stripped; any further space is data.
- ``data`` lines in one event are joined with LF and the result is dispatched
  once, at the blank line.
- A comment line (``:``) is neither data nor an error. It is how servers keep
  intermediaries from closing an idle stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The largest single event this reader will assemble. An unbounded event is
#: how one hostile upstream turns a relay into a memory leak, so exceeding it
#: ends the event with an error rather than growing the buffer.
MAX_EVENT_BYTES = 4 * 1024 * 1024

#: Kept between reads: the tail of a chunk that did not end in a complete line.
_MAX_LINE_BYTES = 1024 * 1024


class SseError(ValueError):
    """Raised when a stream cannot be parsed or has grown past its bound."""


@dataclass(frozen=True)
class SseEvent:
    """One dispatched server-sent event."""

    data: str = ""
    event: str = ""
    #: The stream's last event id, which for MCP is the resumption handle.
    id: str = ""
    #: Reconnection delay in milliseconds, when the server asked for one.
    retry: int | None = None

    def json(self) -> object:
        """Parse the event's data as JSON, which is what MCP carries."""
        import json

        try:
            return json.loads(self.data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SseError(f"this event's data is not JSON: {exc}") from exc


@dataclass
class SseDecoder:
    """Incremental decoder: feed it bytes, receive whole events.

    Incremental rather than line-based because a stream is delivered in chunks
    that split anywhere -- mid-line, mid-CRLF, mid-JSON -- and buffering "until
    the next newline" on a stream that sends progress events is how a relay
    starts adding latency to somebody's tool call.
    """

    _buffer: str = field(default="", init=False)
    _data: list[str] = field(default_factory=list, init=False)
    _event: str = field(default="", init=False)
    _id: str = field(default="", init=False)
    _retry: int | None = field(default=None, init=False)
    #: The last event id seen, which a reconnecting client sends back as
    #: ``Last-Event-ID``. Persisted here because the transport needs it after
    #: the event that carried it has been consumed.
    last_event_id: str = field(default="", init=False)

    def feed(self, chunk: bytes) -> list[SseEvent]:
        """Consume bytes, returning every event that completed."""
        if len(chunk) > MAX_EVENT_BYTES:
            raise SseError("an SSE chunk is larger than this relay will buffer")
        # ``errors="replace"`` rather than strict: a malformed byte in a
        # comment should not take down a stream that is otherwise working, and
        # the JSON events still fail loudly on their own.
        self._buffer += chunk.decode("utf-8", errors="replace")
        events: list[SseEvent] = []
        while True:
            line, separator, remainder = _take_line(self._buffer)
            if not separator:
                break
            self._buffer = remainder
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        if len(self._buffer) > _MAX_LINE_BYTES:
            raise SseError("this stream has a line longer than the relay will buffer")
        return events

    def close(self) -> list[SseEvent]:
        """Flush a stream that ended without a final blank line.

        A stream that ends mid-event has still delivered an event's worth of
        ``data`` in every implementation that matters, and dropping it loses a
        tool result that already happened.
        """
        events: list[SseEvent] = []
        if self._buffer:
            event = self._consume_line(self._buffer)
            if event is not None:
                events.append(event)
            self._buffer = ""
        if self._data:
            events.append(self._dispatch())
        return events

    def _consume_line(self, line: str) -> SseEvent | None:
        """Apply one line, returning an event when the line completes one."""
        if line == "":
            return self._dispatch() if self._data or self._event or self._id else None
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        match name:
            case "data":
                self._data.append(value)
            case "event":
                self._event = value
            case "id":
                # A NUL in an id is defined to be ignored, which is how a
                # server resets the id without ending the stream.
                if "\x00" not in value:
                    self._id = value
            case "retry":
                if value.isdigit():
                    self._retry = int(value)
            case _:
                # Unknown fields are ignored by design.
                return None
        return None

    def _dispatch(self) -> SseEvent:
        """Build the event the accumulated fields describe, and reset."""
        event = SseEvent(
            data="\n".join(self._data),
            event=self._event,
            id=self._id,
            retry=self._retry,
        )
        if event.id:
            self.last_event_id = event.id
        self._data = []
        self._event = ""
        self._retry = None
        return event


def _take_line(buffer: str) -> tuple[str, str, str]:
    """Split off the first complete line, whatever terminator it used."""
    for index, character in enumerate(buffer):
        if character == "\n":
            return buffer[:index], "\n", buffer[index + 1 :]
        if character == "\r":
            # CRLF: the LF belongs to the terminator, not the next line.
            if index + 1 < len(buffer):
                if buffer[index + 1] == "\n":
                    return buffer[:index], "\r\n", buffer[index + 2 :]
                return buffer[:index], "\r", buffer[index + 1 :]
            # A CR at the very end may be the first half of a CRLF that has not
            # arrived yet; wait for the next chunk rather than guessing.
            return "", "", buffer
    return "", "", buffer


def iter_events(chunks: Iterator[bytes]) -> Iterator[SseEvent]:
    """Decode a whole stream lazily, for callers that are not incremental."""
    decoder = SseDecoder()
    for chunk in chunks:
        yield from decoder.feed(chunk)
    yield from decoder.close()


__all__ = [
    "MAX_EVENT_BYTES",
    "SseDecoder",
    "SseError",
    "SseEvent",
    "iter_events",
]
