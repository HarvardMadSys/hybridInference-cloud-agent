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

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The largest single event this reader will assemble. An unbounded event is
#: how one hostile upstream turns a relay into a memory leak, so exceeding it
#: ends the event with an error rather than growing the buffer.
MAX_EVENT_BYTES = 4 * 1024 * 1024

#: The longest line this reader will hold, complete or still arriving. As long
#: as an event may be, because servers write each JSON-RPC message as a single
#: ``data`` line: a shorter bound here would refuse events the event bound
#: allows.
_MAX_LINE_BYTES = MAX_EVENT_BYTES

#: Any of the three line terminators. CRLF comes first so that it is taken as
#: one terminator rather than as a CR followed by an empty line.
_LINE_END = re.compile(rb"\r\n|\r|\n")

#: Bound conversion work and the requested delay to signed 32-bit milliseconds.
_MAX_RETRY_MS = 2_147_483_647
_MAX_RETRY_DIGITS = 10


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
        except RecursionError as exc:
            # Deep enough nesting exhausts the parser's recursion, which is a
            # malformed event like any other rather than a crash of the reader.
            raise SseError("this event's data nests deeper than this platform parses") from exc


@dataclass
class SseDecoder:
    """Incremental decoder: feed it bytes, receive whole events.

    Incremental rather than line-based because a stream is delivered in chunks
    that split anywhere -- mid-line, mid-CRLF, mid-JSON -- and buffering "until
    the next newline" on a stream that sends progress events is how a relay
    starts adding latency to somebody's tool call.

    **Every byte is examined once.** The search for a line's end resumes where
    the previous chunk's search stopped, rather than at the start of the line
    still being assembled: rescanning that line on every chunk made one long
    line delivered in small pieces quadratic, and the server chooses both the
    line and the pieces. Lines are split as bytes and decoded whole, so a
    character whose bytes straddle two chunks is not torn in half either.
    """

    _buffer: bytearray = field(default_factory=bytearray, init=False)
    #: Where the search for the next line terminator resumes in ``_buffer``.
    _scanned: int = field(default=0, init=False)
    _data: list[bytes] = field(default_factory=list, init=False)
    _data_bytes: int = field(default=0, init=False)
    _event: str = field(default="", init=False)
    _id: str = field(default="", init=False)
    #: The received sizes of the two fields above, kept so that bounding an
    #: event costs the same however many lines it has.
    _event_bytes: int = field(default=0, init=False)
    _id_bytes: int = field(default=0, init=False)
    _retry: int | None = field(default=None, init=False)
    #: The last event id seen, which a reconnecting client sends back as
    #: ``Last-Event-ID``. Persisted here because the transport needs it after
    #: the event that carried it has been consumed.
    last_event_id: str = field(default="", init=False)

    def feed(self, chunk: bytes) -> list[SseEvent]:
        """Consume bytes, returning every event that completed."""
        if len(chunk) > MAX_EVENT_BYTES:
            raise SseError("an SSE chunk is larger than this relay will buffer")
        buffer = self._buffer
        buffer += chunk
        events: list[SseEvent] = []
        start = 0
        while True:
            terminator = _LINE_END.search(buffer, self._scanned)
            if terminator is None:
                self._scanned = len(buffer)
                break
            if terminator.group() == b"\r" and terminator.end() == len(buffer):
                # A CR at the very end may be the first half of a CRLF that has
                # not arrived yet; wait for the next chunk rather than guessing.
                self._scanned = terminator.start()
                break
            event = self._consume_line(bytes(buffer[start : terminator.start()]))
            start = self._scanned = terminator.end()
            if event is not None:
                events.append(event)
        if start:
            del buffer[:start]
            self._scanned -= start
        if len(buffer) > _MAX_LINE_BYTES:
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
            # A CR held back in case an LF followed is a terminator after all:
            # nothing can follow it now.
            line = bytes(self._buffer).removesuffix(b"\r")
            self._buffer.clear()
            self._scanned = 0
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        if self._data:
            events.append(self._dispatch())
        return events

    def _consume_line(self, line: bytes) -> SseEvent | None:
        """Apply one line, returning an event when the line completes one."""
        if len(line) > _MAX_LINE_BYTES:
            raise SseError("this stream has a line longer than the relay will buffer")
        if not line:
            return self._dispatch() if self._data or self._event or self._id else None
        if line.startswith(b":"):
            return None
        name, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        match name:
            case b"data":
                size = len(value) + 1
                self._check_event_size(data_bytes=self._data_bytes + size)
                self._data.append(value)
                self._data_bytes += size
            case b"event":
                self._check_event_size(event_bytes=len(value))
                self._event = _text(value)
                self._event_bytes = len(value)
            case b"id":
                # A NUL in an id is defined to be ignored, which is how a
                # server resets the id without ending the stream.
                if b"\x00" not in value:
                    self._check_event_size(id_bytes=len(value))
                    self._id = _text(value)
                    self._id_bytes = len(value)
            case b"retry":
                # SSE requires ASCII decimal digits, which is all ``bytes.isdigit``
                # accepts. A decoded ``str.isdigit`` would also accept characters
                # such as superscript two that int rejects.
                if value.isdigit():
                    if len(value) > _MAX_RETRY_DIGITS:
                        raise SseError("this stream has a retry delay beyond the relay limit")
                    delay = int(value)
                    if delay > _MAX_RETRY_MS:
                        raise SseError("this stream has a retry delay beyond the relay limit")
                    self._retry = delay
            case _:
                # Unknown fields are ignored by design.
                return None
        return None

    def _check_event_size(
        self,
        *,
        data_bytes: int | None = None,
        event_bytes: int | None = None,
        id_bytes: int | None = None,
    ) -> None:
        """Bound retained fields across all lines and chunks of one event."""
        size = self._data_bytes if data_bytes is None else data_bytes
        size += self._event_bytes if event_bytes is None else event_bytes
        size += self._id_bytes if id_bytes is None else id_bytes
        if size > MAX_EVENT_BYTES:
            raise SseError("an SSE event is larger than this relay will buffer")

    def _dispatch(self) -> SseEvent:
        """Build the event the accumulated fields describe, and reset."""
        event = SseEvent(
            data=_text(b"\n".join(self._data)),
            event=self._event,
            id=self._id,
            retry=self._retry,
        )
        if event.id:
            self.last_event_id = event.id
        self._data = []
        self._data_bytes = 0
        self._event = ""
        self._event_bytes = 0
        self._retry = None
        return event


def _text(value: bytes) -> str:
    """Decode one field's value.

    ``errors="replace"`` rather than strict: a malformed byte in a comment or an
    event name should not take down a stream that is otherwise working, and the
    JSON events still fail loudly on their own.
    """
    return value.decode("utf-8", errors="replace")


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
