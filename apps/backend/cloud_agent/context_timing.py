"""A conversation's Timing Stats: where the executions' time went.

Adapted from bowenliang123/dsh-context's ``statsTiming.tsx`` and
``timingStrip.tsx`` (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001):
the categories (TTFT, thinking, answers, tool arguments, tool runs, the rest),
the active-time total and the strip are theirs; the measurements are ours.

Every execution (attempt) contributes its own span only -- first to last
event as the platform received them -- so the wait before a retry belongs to
no one. Inside a span, each instant gets one label, in this order:

1. **TTFT** -- inside a model request, before its first token, as the Gateway
   measured it (``request_started_at`` + ``ttft_ms``);
2. **Generation** -- inside a model request after its first token (to
   ``request_started_at`` + ``latency_ms``). Claude Code's streamed block
   durations (``block_ms``, this release on) split it into thinking, answers
   and tool arguments, each at most what is left of it;
3. **Tool runs** -- inside a tool window (``tool_use`` to its result) and no
   model request; a call without a result has no window and is counted apart;
4. **Not attributed** -- the rest of the span: the harness, waits, and the
   lag of event receipt. Not called overhead: nothing measured it.

An execution the Gateway rows do not cover (older than the bounded read, an
unreachable Gateway, a runtime connection the platform does not route) has
no model intervals: its span minus tool runs is **model + harness (not
split)**. Overlaps count once, so no category can exceed the span.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_profile import profile_is_current
from cloud_agent.context_running import merge_intervals

if TYPE_CHECKING:
    from cloud_agent.grant_usage import GrantUsageRequestPage

TimingKind = Literal["ttft", "reasoning", "text", "toolarg", "gen", "tools", "unsplit", "other"]
TIMING_KINDS: tuple[TimingKind, ...] = (
    "ttft",
    "reasoning",
    "text",
    "toolarg",
    "gen",
    "tools",
    "unsplit",
    "other",
)
#: Strip segments one response carries; adjacent same-kind segments merge first.
MAX_STRIP_SEGMENTS = 1_200


class TimingSegment(BaseModel):
    """One stretch of an execution with a single label (the strip)."""

    model_config = ConfigDict(extra="forbid")

    kind: TimingKind
    start_ms: int
    end_ms: int


class RequestTiming(BaseModel):
    """One model request the Gateway timed, by the id the CLI reported for it."""

    model_config = ConfigDict(extra="forbid")

    response_id: str
    ttft_ms: int | None = None
    latency_ms: int | None = None


class ThreadTiming(BaseModel):
    """Where the executions' time went, and how much of it could be split."""

    model_config = ConfigDict(extra="forbid")

    #: Sum of execution spans: the donut's whole.
    active_ms: int = Field(ge=0)
    totals: dict[TimingKind, int]
    executions: int = Field(ge=0)
    #: Executions whose model time the Gateway rows split.
    executions_split: int = Field(ge=0)
    model_requests: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    #: Calls without a result (crashed, cancelled, still running): no span.
    tool_calls_unpaired: int = Field(ge=0)
    #: Claude Code blocks with a streamed duration, by kind.
    blocks: dict[str, int] = Field(default_factory=dict)
    #: Output tokens per second of Gateway-measured generation; None without both.
    output_tps: float | None = None
    gateway: Literal["available", "partial", "unavailable", "not_recorded"]
    gateway_reason: str | None = None
    segments: list[TimingSegment]
    segments_merged: bool = False
    #: Requests timed by the Gateway, joinable to a point's ``usage_id``.
    requests: list[RequestTiming] = Field(default_factory=list)


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


def _length(intervals: Sequence[list[int]]) -> int:
    return sum(end - start for start, end in intervals)


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
        self.blocks = {
            kind: int((blocks.get(kind) or {}).get("ms") or 0)
            for kind in ("reasoning", "text", "toolarg")
        }
        self.block_counts = {
            kind: int((blocks.get(kind) or {}).get("blocks") or 0)
            for kind in ("reasoning", "text", "toolarg")
        }
        self.ttft: list[list[int]] = []
        self.model: list[list[int]] = []
        self.split = False


def _requests_of(page: GrantUsageRequestPage) -> list[tuple[str, str | None, int, int, int, int]]:
    """(request id, response id, start, ttft, end, output) of each timed request."""
    rows = []
    for request in page.requests:
        if request.ttft_ms is None or request.latency_ms is None:
            continue
        if request.status_code is not None and request.status_code >= 400:
            continue
        start = int(request.request_started_at.timestamp() * 1000)
        ttft = min(int(request.ttft_ms), int(request.latency_ms))
        rows.append(
            (
                request.request_id,
                request.response_id,
                start,
                start + ttft,
                start + int(request.latency_ms),
                int(request.tokens_out or 0),
            )
        )
    return rows


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


class _Requests:
    """The Gateway's timed requests, each attributed to the execution that sent it."""

    def __init__(
        self,
        executions: Mapping[int, _Execution],
        pages: Sequence[tuple[Mapping[str, Any], GrantUsageRequestPage]],
    ) -> None:
        seen: set[str] = set()
        self.joined: list[RequestTiming] = []
        self.count = 0
        self.output_tokens = 0
        self.generation_ms = 0
        for row, page in pages:
            execution = executions.get(int(row.get("attempt_id") or 0))
            if execution is None:
                continue
            execution.split = True
            for request_id, response_id, start, first, end, out in _requests_of(page):
                if request_id in seen:
                    continue
                seen.add(request_id)
                self.count += 1
                execution.ttft.append([start, first])
                execution.model.append([start, end])
                if out > 0 and end > first:
                    self.output_tokens += out
                    self.generation_ms += end - first
                if response_id:
                    self.joined.append(
                        RequestTiming(
                            response_id=response_id, ttft_ms=first - start, latency_ms=end - start
                        )
                    )


def _label(
    execution: _Execution, totals: dict[TimingKind, int]
) -> list[tuple[TimingKind, int, int]]:
    """One execution's labelled stretches; its totals added to ``totals``."""
    start, end = execution.start, execution.end
    model = merge_intervals(_clip(execution.model, start, end))
    ttft = merge_intervals(_clip(execution.ttft, start, end))
    generation = _interval_minus(model, ttft)
    tools = _interval_minus(_clip(execution.tools, start, end), model)
    labelled: list[tuple[TimingKind, list[int]]] = [
        *(("ttft", interval) for interval in ttft),
        *(("gen", interval) for interval in generation),
        *(("tools", interval) for interval in tools),
    ]
    busy = merge_intervals([interval for _, interval in labelled])
    rest: TimingKind = "other" if execution.split else "unsplit"
    labelled += [(rest, interval) for interval in _interval_minus([[start, end]], busy)]
    segments: list[tuple[TimingKind, int, int]] = []
    for kind, (a, b) in sorted(labelled, key=lambda item: item[1][0]):
        if b > a:
            segments.append((kind, a, b))
            totals[kind] += b - a
    # Streamed block time divides generation (or, unsplit, the model and
    # harness time), never more than there is.
    target: TimingKind = "gen" if execution.split else "unsplit"
    pool = totals_of(segments, target)
    for kind in ("reasoning", "text", "toolarg"):
        take = min(execution.blocks[kind], pool)
        totals[kind] += take
        totals[target] -= take
        pool -= take
    return segments


def totals_of(segments: Sequence[tuple[TimingKind, int, int]], kind: TimingKind) -> int:
    """Milliseconds of one kind in labelled stretches."""
    return sum(b - a for k, a, b in segments if k == kind)


def assemble_timing(
    attempts: Sequence[Mapping[str, Any]],
    pages: Sequence[tuple[Mapping[str, Any], GrantUsageRequestPage]],
    *,
    gateway_gap: str | None,
    gateway_started: int,
) -> ThreadTiming:
    """Fold the attempts' recorded timing and the Gateway's request rows."""
    executions = _executions(attempts)
    requests = _Requests(executions, pages)
    totals: dict[TimingKind, int] = dict.fromkeys(TIMING_KINDS, 0)
    segments: list[tuple[TimingKind, int, int]] = []
    ordered = sorted(executions.values(), key=lambda item: item.start)
    for execution in ordered:
        segments += _label(execution, totals)
    bounded, merged = _bounded_segments(segments)
    timed = [e for e in ordered if e.split]
    gateway: Literal["available", "partial", "unavailable", "not_recorded"]
    if not executions or gateway_started == 0:
        gateway = "not_recorded"
    elif not timed:
        gateway = "unavailable"
    elif len(timed) < len(executions) or gateway_gap is not None:
        gateway = "partial"
    else:
        gateway = "available"
    return ThreadTiming(
        active_ms=sum(e.end - e.start for e in ordered),
        totals=totals,
        executions=len(ordered),
        executions_split=len(timed),
        model_requests=requests.count,
        tool_calls=sum(e.tool_calls for e in ordered),
        tool_calls_unpaired=sum(e.unpaired for e in ordered),
        blocks={
            kind: sum(e.block_counts[kind] for e in ordered)
            for kind in ("reasoning", "text", "toolarg")
        },
        output_tps=(
            requests.output_tokens / (requests.generation_ms / 1000)
            if requests.generation_ms
            else None
        ),
        gateway=gateway,
        gateway_reason=gateway_gap,
        segments=[TimingSegment(kind=k, start_ms=a, end_ms=b) for k, a, b in bounded],
        segments_merged=merged,
        requests=requests.joined[:2_000],
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
