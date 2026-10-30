"""What one attempt's profile keeps per request and over time, bounded.

Two small accumulators the context profile reducer feeds while it reads an
attempt's events in order (:mod:`cloud_agent.context_profile`):

- :class:`RunningContent` -- the stored content recorded *before* each model
  request, by category, as a token estimate (``chars_cjk_v1``). A completed
  compaction starts it over, as the CLI's next request carries the summary
  instead of what came before. A request's own output is never part of it:
  Claude Code reports a request's usage on the first block of its answer
  (read before that block is added); Codex, pi, OpenCode and Kilo report it
  after the answer, so the output recorded since the previous request is
  taken back out of that request's vector (it is input to the next one).
- :class:`RunningTiming` -- the attempt's span (first to last event, as the
  platform received them) and the union of its tool windows (``tool_use``
  to its ``tool_result``), the same pairing ``cloud_agent.thread_stats``
  uses. Intervals are merged; past a bound the smallest gaps are closed and
  the time that added is recorded.

Neither reads anything but the events the reducer is already given.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.text_tokens import estimate_from_counts

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The categories a request's vector holds, in order.
POINT_CATEGORIES: tuple[str, ...] = (
    "user",
    "inject",
    "skill",
    "summary",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
)
_INDEX = {key: index for index, key in enumerate(POINT_CATEGORIES)}
#: Profile content categories, as the vector names them.
CONTENT_TO_POINT: dict[str, str] = {
    "user_messages": "user",
    "injected": "inject",
    "skills": "skill",
    "compaction_summary": "summary",
    "assistant": "assistant",
    "reasoning": "reasoning",
    "tool_calls": "tool_calls",
    "tool_results": "tool_results",
}
_OUTPUT = (_INDEX["assistant"], _INDEX["reasoning"], _INDEX["tool_calls"])

#: Tool-window intervals one attempt keeps; past it the smallest gaps close.
MAX_TOOL_INTERVALS = 300
#: Calls one attempt tracks while they wait for their result.
MAX_OPEN_CALLS = 2_000


def event_tokens(payload: Mapping[str, Any], *, chars_key: str = "chars") -> int:
    """The estimate for one event's recorded text, from its character counts."""
    chars = _int(payload.get(chars_key))
    if chars <= 0:
        return 0
    ascii_chars, cjk = _int(payload.get("ascii")), _int(payload.get("cjk"))
    return estimate_from_counts(ascii_chars, cjk, chars - ascii_chars - cjk)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class RunningContent:
    """Recorded content since the attempt started, or since its last compaction."""

    def __init__(self) -> None:
        self.local = [0] * len(POINT_CATEGORIES)
        self._output_since_point = [0] * len(POINT_CATEGORIES)
        #: A completed compaction happened in this attempt (content restarted).
        self.compacted = False

    def add(self, content_category: str, tokens: int) -> None:
        """Count ``tokens`` of one stored item in its category."""
        key = CONTENT_TO_POINT.get(content_category)
        if key is None or tokens <= 0:
            return
        index = _INDEX[key]
        self.local[index] += tokens
        if index in _OUTPUT:
            self._output_since_point[index] += tokens

    def compact(self) -> None:
        """A completed compaction: what came before is no longer carried."""
        self.local = [0] * len(POINT_CATEGORIES)
        self._output_since_point = [0] * len(POINT_CATEGORIES)
        self.compacted = True

    def point(self, *, output_after_usage: bool) -> list[int]:
        """The vector for a request just reported; output is not its own input."""
        vector = list(self.local)
        if output_after_usage:
            vector = [
                max(0, value - own)
                for value, own in zip(vector, self._output_since_point, strict=True)
            ]
        self._output_since_point = [0] * len(POINT_CATEGORIES)
        return vector


def _ms(value: Any) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
    return None


def merge_intervals(intervals: list[list[int]]) -> list[list[int]]:
    """The union of ``[start, end]`` intervals, sorted, overlaps counted once."""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def bound_intervals(intervals: list[list[int]], limit: int) -> tuple[list[list[int]], int]:
    """At most ``limit`` intervals, closing the smallest gaps; the ms that added."""
    merged = merge_intervals(intervals)
    added = 0
    while len(merged) > limit:
        gaps = [(merged[i + 1][0] - merged[i][1], i) for i in range(len(merged) - 1)]
        gap, index = min(gaps)
        added += gap
        merged[index] = [merged[index][0], merged[index + 1][1]]
        del merged[index + 1]
    return merged, added


class RunningTiming:
    """An attempt's event span and tool windows, as the platform received them."""

    def __init__(self) -> None:
        self.first_ms: int | None = None
        self.last_ms: int | None = None
        self._open: dict[str, int] = {}
        self._windows: list[list[int]] = []
        self.tool_calls = 0

    def event(self, event_type: str, created_at: Any, tool_id: Any) -> None:
        """Note one event's time, and open or close a tool window."""
        at = _ms(created_at)
        if at is None:
            return
        self.first_ms = at if self.first_ms is None else min(self.first_ms, at)
        self.last_ms = at if self.last_ms is None else max(self.last_ms, at)
        if not isinstance(tool_id, str) or not tool_id:
            return
        if event_type == "tool_use":
            self.tool_calls += 1
            if len(self._open) < MAX_OPEN_CALLS:
                self._open[tool_id] = at
        elif event_type == "tool_result":
            started = self._open.pop(tool_id, None)
            if started is not None and at >= started:
                self._windows.append([started, at])
                if len(self._windows) > 4 * MAX_TOOL_INTERVALS:
                    self._windows = merge_intervals(self._windows)

    def finish(self) -> dict[str, Any] | None:
        """The stored timing record; None for an attempt without timed events."""
        if self.first_ms is None or self.last_ms is None:
            return None
        windows, added = bound_intervals(self._windows, MAX_TOOL_INTERVALS)
        return {
            "span": [self.first_ms, self.last_ms],
            "tools": windows,
            "tools_gap_closed_ms": added,
            "tool_calls": self.tool_calls,
            # A call without a result (crashed, cancelled, still running)
            # contributes no window rather than an open-ended one.
            "tools_unpaired": len(self._open),
        }
