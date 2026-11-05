"""Merging a native stream's adjacent per-token deltas into fewer stored rows."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cloud_agent_host.runtimes import ReplyPreview

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.drivers import HarnessEvent


@dataclass(frozen=True)
class StreamDelta:
    """One native record that only adds a few characters to a stream.

    ``path`` leads to the added text inside ``value``; ``render`` turns a
    record and its line into the row the platform stores for it.
    """

    value: dict[str, Any]
    path: tuple[str, ...]
    delta: str
    line: str
    render: Callable[[dict[str, Any], str], HarnessEvent]


def _with_delta(value: dict[str, Any], path: tuple[str, ...], delta: str) -> dict[str, Any]:
    """A copy of ``value`` whose text at ``path`` is ``delta``."""
    head, *rest = path
    inner = value.get(head)
    if rest and isinstance(inner, dict):
        return {**value, head: _with_delta(inner, tuple(rest), delta)}
    return {**value, head: delta}


def _escaped_chars(text: str) -> int:
    """How many characters ``text`` takes inside a JSON string."""
    return len(json.dumps(text)) - 2


class DeltaBatcher:
    """Merge adjacent per-token deltas of one native stream into fewer rows.

    Codex's reasoning and command-output deltas and OpenCode/Kilo's reasoning
    deltas arrive a few characters at a time, and every row the runner gets is
    a stored event and a synchronous control-plane request. Only *adjacent*
    deltas of the *same* stream are merged, so no text is dropped or
    reordered: the first piece of a quiet stream goes out at once, later ones
    at most once per :attr:`INTERVAL_S`, and any other protocol line -- the
    item's completion, the turn's boundary, an interrupt's settlement --
    first flushes what is gathered (see ``_JsonResidentTurn.events_for_line``).
    """

    INTERVAL_S = ReplyPreview.INTERVAL_S
    #: A merged row's line stays within the 4,000 characters a raw row keeps
    #: of its line, so merging never clips text that separate rows kept whole.
    MAX_LINE_CHARS = 4000

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        """``clock`` is for tests; it defaults to ``time.monotonic``."""
        self._clock = clock or time.monotonic
        self._first: StreamDelta | None = None
        self._key = ""
        self._pieces: list[str] = []
        self._line_chars = 0
        self._reported_at: dict[str, float] = {}

    @property
    def holding(self) -> bool:
        """Whether the latest delta is gathered rather than reported yet."""
        return self._first is not None

    def add(self, item: StreamDelta) -> list[HarnessEvent]:
        """Gather ``item``; return the rows now due, in stream order."""
        empty = _with_delta(item.value, item.path, "")
        key = json.dumps(empty, sort_keys=True)
        added = _escaped_chars(item.delta)
        rows: list[HarnessEvent] = []
        if self._first is not None and (
            key != self._key or self._line_chars + added > self.MAX_LINE_CHARS
        ):
            rows.extend(self.flush())
        now = self._clock()
        if self._first is None:
            reported_at = self._reported_at.get(key)
            if reported_at is None or now - reported_at >= self.INTERVAL_S:
                self._reported_at[key] = now
                rows.append(item.render(item.value, item.line))
                return rows
            self._first, self._key = item, key
            self._pieces = [item.delta]
            self._line_chars = len(json.dumps(empty, separators=(",", ":"))) + added
            return rows
        self._pieces.append(item.delta)
        self._line_chars += added
        if now - self._reported_at[key] >= self.INTERVAL_S:
            rows.extend(self.flush())
        return rows

    def flush(self) -> list[HarnessEvent]:
        """Report whatever is gathered, as one row."""
        first = self._first
        if first is None:
            return []
        if len(self._pieces) == 1:
            value, line = first.value, first.line
        else:
            value = _with_delta(first.value, first.path, "".join(self._pieces))
            line = json.dumps(value, separators=(",", ":"))
        self._reported_at[self._key] = self._clock()
        self._first, self._key, self._pieces, self._line_chars = None, "", [], 0
        return [first.render(value, line)]
