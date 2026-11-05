"""A conversation's Timing Stats: where the executions' time went.

Adapted from bowenliang123/dsh-context's ``statsTiming.tsx`` and
``timingStrip.tsx`` (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001):
the categories, the active-time total and the strip are theirs; the
measurements are ours.

Every execution (attempt) contributes its own span only -- first to last
event as the platform received them -- so the wait before a retry belongs to
no one. Inside a span, each instant gets one label:

1. **Tool runs** -- inside a tool window (``tool_use`` to its result); a call
   without a result has no window and is counted apart;
2. **Model + harness (not split)** -- the rest of the span. No request log is
   recorded for an owner's own model connection, so nothing separates model
   time from the harness, waits and the lag of event receipt. Claude Code's
   streamed block durations (``block_ms``) divide it into thinking, answers
   and tool arguments, each at most what is left of it.

Overlaps count once, so no category can exceed the span.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context.context_profile import profile_is_current
from cloud_agent.context.context_running import merge_intervals

TimingKind = Literal["reasoning", "text", "toolarg", "tools", "unsplit"]
TIMING_KINDS: tuple[TimingKind, ...] = ("reasoning", "text", "toolarg", "tools", "unsplit")
#: Kinds Claude Code's streamed block durations carve out of the unsplit time.
_BLOCK_KINDS: tuple[Literal["reasoning", "text", "toolarg"], ...] = ("reasoning", "text", "toolarg")
#: Strip segments one response carries; adjacent same-kind segments merge first.
MAX_STRIP_SEGMENTS = 1_200


class TimingSegment(BaseModel):
    """One stretch of an execution with a single label (the strip)."""

    model_config = ConfigDict(extra="forbid")

    kind: TimingKind
    start_ms: int
    end_ms: int


class ThreadTiming(BaseModel):
    """Where the executions' time went."""

    model_config = ConfigDict(extra="forbid")

    #: Sum of execution spans: the donut's whole.
    active_ms: int = Field(ge=0)
    totals: dict[TimingKind, int]
    executions: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    #: Calls without a result (crashed, cancelled, still running): no span.
    tool_calls_unpaired: int = Field(ge=0)
    #: Claude Code blocks with a streamed duration, by kind.
    blocks: dict[str, int] = Field(default_factory=dict)
    segments: list[TimingSegment]
    segments_merged: bool = False
    # COMPAT(remove-after=2026-12-31): no browser tab still runs a frontend that
    # reads them. Retired Gateway timing, always these values; a tab opened before
    # the frontend that stopped reading them must not fail on their absence.
    executions_split: int = 0
    model_requests: int = 0
    output_tps: float | None = None
    gateway: Literal["not_recorded"] = "not_recorded"
    gateway_reason: str | None = None
    requests: list[dict[str, Any]] = Field(default_factory=list)


def _interval_minus(intervals: Sequence[list[int]], cut: Sequence[list[int]]) -> list[list[int]]:
    """``intervals`` with ``cut`` removed (both merged and sorted)."""
    result: list[list[int]] = []
    for start, end in intervals:
        cursor = start
        for cut_start, cut_end in cut:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append([cursor, cut_start])
            cursor = max(cursor, cut_end)
        if cursor < end:
            result.append([cursor, end])
    return result


def _clip(intervals: Sequence[list[int]], start: int, end: int) -> list[list[int]]:
    return [[max(a, start), min(b, end)] for a, b in intervals if b > start and a < end]


class _Execution:
    def __init__(self, attempt_id: int, timing: Mapping[str, Any]) -> None:
        span = timing.get("span") or [0, 0]
        self.attempt_id = attempt_id
        self.start, self.end = int(span[0]), int(span[1])
        self.tools = merge_intervals(
            [[int(a), int(b)] for a, b in timing.get("tools") or [] if int(b) >= int(a)]
        )
        self.tool_calls = int(timing.get("tool_calls") or 0)
        self.unpaired = int(timing.get("tools_unpaired") or 0)
        blocks = timing.get("blocks") or {}
        self.blocks = {kind: int((blocks.get(kind) or {}).get("ms") or 0) for kind in _BLOCK_KINDS}
        self.block_counts = {
            kind: int((blocks.get(kind) or {}).get("blocks") or 0) for kind in _BLOCK_KINDS
        }


def _executions(attempts: Sequence[Mapping[str, Any]]) -> dict[int, _Execution]:
    executions: dict[int, _Execution] = {}
    for attempt in attempts:
        profile = attempt.get("profile")
        if not isinstance(profile, Mapping) or not profile_is_current(profile):
            continue
        timing = profile.get("timing")
        if isinstance(timing, Mapping) and timing.get("span"):
            execution = _Execution(int(attempt["attempt_id"]), timing)
            executions[execution.attempt_id] = execution
    return executions


def _label(
    execution: _Execution, totals: dict[TimingKind, int]
) -> list[tuple[TimingKind, int, int]]:
    """One execution's labelled stretches; its totals added to ``totals``."""
    start, end = execution.start, execution.end
    tools = _clip(execution.tools, start, end)
    labelled: list[tuple[TimingKind, list[int]]] = [("tools", interval) for interval in tools]
    busy = merge_intervals([interval for _, interval in labelled])
    labelled += [("unsplit", interval) for interval in _interval_minus([[start, end]], busy)]
    segments: list[tuple[TimingKind, int, int]] = []
    for kind, (a, b) in sorted(labelled, key=lambda item: item[1][0]):
        if b > a:
            segments.append((kind, a, b))
            totals[kind] += b - a
    # Streamed block time divides the model and harness time, never more than there is.
    pool = totals_of(segments, "unsplit")
    for kind in _BLOCK_KINDS:
        take = min(execution.blocks[kind], pool)
        totals[kind] += take
        totals["unsplit"] -= take
        pool -= take
    return segments


def totals_of(segments: Sequence[tuple[TimingKind, int, int]], kind: TimingKind) -> int:
    """Milliseconds of one kind in labelled stretches."""
    return sum(b - a for k, a, b in segments if k == kind)


def assemble_timing(attempts: Sequence[Mapping[str, Any]]) -> ThreadTiming:
    """Fold the attempts' recorded timing."""
    executions = _executions(attempts)
    totals: dict[TimingKind, int] = dict.fromkeys(TIMING_KINDS, 0)
    segments: list[tuple[TimingKind, int, int]] = []
    ordered = sorted(executions.values(), key=lambda item: item.start)
    for execution in ordered:
        segments += _label(execution, totals)
    bounded, merged = _bounded_segments(segments)
    return ThreadTiming(
        active_ms=sum(e.end - e.start for e in ordered),
        totals=totals,
        executions=len(ordered),
        tool_calls=sum(e.tool_calls for e in ordered),
        tool_calls_unpaired=sum(e.unpaired for e in ordered),
        blocks={kind: sum(e.block_counts[kind] for e in ordered) for kind in _BLOCK_KINDS},
        segments=[TimingSegment(kind=k, start_ms=a, end_ms=b) for k, a, b in bounded],
        segments_merged=merged,
    )


def _bounded_segments(
    segments: list[tuple[TimingKind, int, int]],
) -> tuple[list[tuple[TimingKind, int, int]], bool]:
    """Adjacent same-kind segments merged; past the bound, the shortest folded into a neighbour."""
    merged: list[tuple[TimingKind, int, int]] = []
    for kind, start, end in segments:
        if merged and merged[-1][0] == kind and merged[-1][2] == start:
            merged[-1] = (kind, merged[-1][1], end)
        else:
            merged.append((kind, start, end))
    if len(merged) <= MAX_STRIP_SEGMENTS:
        return merged, False
    # Keep the longest segments; the strip draws a gradient past 600 anyway.
    keep = sorted(range(len(merged)), key=lambda i: merged[i][2] - merged[i][1], reverse=True)
    chosen = sorted(keep[:MAX_STRIP_SEGMENTS])
    return [merged[i] for i in chosen], True
