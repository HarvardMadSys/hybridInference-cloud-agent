"""Byte-bounded pipe reads and cancellable producer backpressure."""

from __future__ import annotations

import codecs
import os
import queue
import threading
from collections import deque
from typing import TYPE_CHECKING, Generic, TypeVar

from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO

OUTPUT_CHUNK_BYTES = 64 * 1024
MAX_PROTOCOL_LINE_BYTES = 8 * 1024 * 1024
STDERR_TAIL_BYTES = 256 * 1024
OUTPUT_QUEUE_ITEMS = 4
_T = TypeVar("_T")


def text_chunks(pipe: IO[str]) -> Iterator[str]:
    """Read pipes without readline's unlimited accumulation before a newline."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while data := os.read(pipe.fileno(), OUTPUT_CHUNK_BYTES):
        chunk = decoder.decode(data)
        if chunk:
            yield chunk
    if final := decoder.decode(b"", final=True):
        yield final


def protocol_lines(
    chunks: Iterator[str], *, max_bytes: int = MAX_PROTOCOL_LINE_BYTES
) -> Iterator[str]:
    """Preserve line framing while refusing an oversized native CLI record."""
    pending: list[str] = []
    pending_bytes = 0
    for chunk in chunks:
        # NDJSON is LF-framed. Unicode line separators, VT, and FF can occur
        # inside JSON strings and must never become extra protocol records.
        pieces = chunk.split("\n")
        for index, piece in enumerate(pieces):
            terminated = index < len(pieces) - 1
            if terminated:
                piece += "\n"
            if not piece:
                continue
            pending_bytes += len(piece.encode("utf-8"))
            if pending_bytes > max_bytes:
                raise SandboxError("sandbox output record exceeds the byte limit")
            pending.append(piece)
            if terminated:
                line = "".join(pending)
                yield line[:-2] + "\n" if line.endswith("\r\n") else line
                pending = []
                pending_bytes = 0
    if pending:
        yield "".join(pending)


class TextTail:
    """Keep at most a fixed number of encoded diagnostic bytes."""

    def __init__(self, limit: int = STDERR_TAIL_BYTES) -> None:
        self.limit = limit
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        """Append diagnostic text while trimming older bytes."""
        chunk = text.encode("utf-8", "replace")[-self.limit :]
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > self.limit:
                first = self._chunks.popleft()
                excess = self._size - self.limit
                if len(first) > excess:
                    self._chunks.appendleft(first[excess:])
                    self._size = self.limit
                else:
                    self._size -= len(first)

    def text(self) -> str:
        """Return the retained diagnostic tail."""
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", "replace")


def put_until_stopped(into: queue.Queue[_T], value: _T, stop: threading.Event) -> bool:
    """Bound queues without leaving abandoned producer threads blocked on put."""
    while not stop.is_set():
        try:
            into.put(value, timeout=0.1)
            return True
        except queue.Full:
            continue
    return False


class OutputPump(Generic[_T]):
    """A bounded producer whose consumer can abandon it without leaking memory."""

    def __init__(self, values: Iterator[_T], *, name: str) -> None:
        self.queue: queue.Queue[_T | Exception | None] = queue.Queue(OUTPUT_QUEUE_ITEMS)
        self.stop = threading.Event()
        self.drain = threading.Event()

        def pump() -> None:
            try:
                for value in values:
                    if (
                        not put_until_stopped(self.queue, value, self.stop)
                        and not self.drain.is_set()
                    ):
                        break
            except Exception as exc:
                put_until_stopped(self.queue, exc, self.stop)
            finally:
                put_until_stopped(self.queue, None, self.stop)

        self.thread = threading.Thread(target=pump, daemon=True, name=name)
        self.thread.start()

    def get(self, timeout: float | None) -> _T | None:
        """Read the next item, propagating a producer failure to its consumer."""
        value = self.queue.get(timeout=timeout)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self, *, drain: bool = False) -> None:
        """Stop queueing; optionally discard output through a graceful EOF flush."""
        if drain:
            self.drain.set()
        self.stop.set()
