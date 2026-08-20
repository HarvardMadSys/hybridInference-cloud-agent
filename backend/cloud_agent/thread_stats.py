"""Derive a thread's aggregate statistics from its stored event ledger.

This module adds no measurement of its own. Every number below is a
projection of rows the platform already writes — ``agent_job_events`` and
``agent_attempts`` — which is why a thread that ran before this code existed
still has a full stats bar.

Two properties are load-bearing, and the rest of the module exists to keep
them true:

**Derived numbers say what they are derived from.** Timing here is
*message-level*: a tool's window is the interval between the ``tool_use``
event and its ``tool_result``, which includes whatever the harness did
between deciding to call the tool and the tool actually running. It is not
execution time, and :attr:`ThreadStats.timing_source` says so, so a caller
cannot render it as if it were.

**A number we do not have is absent, never zero.** A runtime that reports no
token usage yields ``None``, not ``0`` — the difference between "this model
used no tokens" and "this harness does not tell us" is the whole point of
the field. Callers render ``None`` as an em dash, not as a small number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Event types this module reads. Kept in lockstep with the runner's
# normalized vocabulary (``cloud_agent_host.runtimes``) — a kind that is not
# named here is counted as activity and otherwise ignored, so a new event
# type degrades to "no stats change" rather than to a wrong number.
_TOOL_USE = "tool_use"
_TOOL_RESULT = "tool_result"
_MESSAGE = "message"
_THINKING = "thinking"
_USAGE = "usage"
_LIFECYCLE = "lifecycle"

#: Events that prove the model has started producing this turn's answer.
#: Used for time-to-first-token; a ``lifecycle`` event does not qualify,
#: because the harness emits those before the model has said anything.
_FIRST_OUTPUT_KINDS = frozenset({_MESSAGE, _THINKING, _TOOL_USE})

#: Token-count key spellings seen across runtimes. Claude reports the
#: Anthropic wire shape verbatim; pi's adapter normalizes to its own names;
#: codex has no verified shape yet, so its keys are accepted defensively
#: rather than assumed. An unrecognized shape yields ``None`` (see the
#: module docstring's second property), never a partial sum.
_INPUT_KEYS = ("input_tokens", "input_token_count", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "output_token_count", "completion_tokens")
_CACHE_READ_KEYS = ("cache_read_input_tokens", "cache_read_tokens", "cached_tokens")
_CACHE_WRITE_KEYS = ("cache_creation_input_tokens", "cache_write_tokens")


@dataclass(frozen=True)
class ThreadStats:
    """One thread's aggregate numbers, ready for display.

    Optional fields are ``None`` when the underlying data is unavailable for
    this thread's runtime — never ``0`` as a stand-in.
    """

    #: Turns the user actually sent (jobs in the thread).
    turns: int
    #: Tool calls started, whether or not they finished.
    tool_calls: int
    #: Tool calls whose result carried an error flag.
    failed_tool_calls: int
    #: Wall time from each turn's first to last event, summed over turns.
    wall_seconds: float
    #: Union (never the sum) of tool windows, so parallel calls cannot push
    #: tool time past wall time.
    tool_seconds: float
    #: ``wall_seconds`` minus ``tool_seconds`` — the harness and the model,
    #: which this level of measurement cannot separate from each other.
    model_seconds: float
    #: Mean time from a turn's attempt starting to its first model output.
    #: ``None`` when no turn has both timestamps.
    ttft_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    #: Cached share of all input tokens, from ``0.0`` to ``1.0``. ``None`` when the
    #: runtime reports no cache counters.
    cache_hit_ratio: float | None
    #: Output tokens per second of ``model_seconds``. ``None`` without token
    #: counts or without elapsed model time.
    output_tokens_per_second: float | None
    #: ``"runtime"`` when a runtime reported usage, ``"unavailable"`` when
    #: none did. The UI turns this into the tooltip that explains the dashes.
    tokens_source: str
    #: How the timings above were obtained. Only ``"message_events"`` exists
    #: today; per-tool hook markers would add a more precise value without
    #: changing this field's meaning.
    timing_source: str = "message_events"


def summarize_thread(
    *,
    job_ids: Sequence[str],
    attempts: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> ThreadStats:
    """Summarize one thread from its jobs, attempts and events.

    :param job_ids: every job (turn) in the thread.
    :param attempts: attempt rows carrying ``job_id`` and ``started_at``;
        used only for time-to-first-token, which needs the moment execution
        began rather than the moment the job was queued.
    :param events: the thread's event rows, each with ``job_id``,
        ``event_type``, ``payload`` and ``created_at``. Order does not
        matter; events are grouped and sorted here.
    :returns: the aggregate, with unavailable measures left ``None``.
    """
    by_job: dict[str, list[Mapping[str, Any]]] = {job_id: [] for job_id in job_ids}
    for event in events:
        # A job that vanished from the thread (or an event that arrived for
        # one) is not a reason to fail a read-only summary.
        by_job.setdefault(str(event.get("job_id")), []).append(event)

    first_attempt_start = _earliest_attempt_start_per_job(attempts)

    tool_calls = 0
    failed_tool_calls = 0
    wall_seconds = 0.0
    tool_seconds = 0.0
    ttft_samples: list[float] = []
    totals = _TokenTotals()
    produced_text = False

    for job_id, job_events in by_job.items():
        ordered = sorted(job_events, key=_event_sort_key)
        if not ordered:
            continue

        timestamps = [ts for ts in (_timestamp(event) for event in ordered) if ts is not None]
        if len(timestamps) >= 2:
            wall_seconds += (timestamps[-1] - timestamps[0]).total_seconds()

        tool_calls += sum(1 for event in ordered if event.get("event_type") == _TOOL_USE)
        failed_tool_calls += sum(
            1
            for event in ordered
            if event.get("event_type") == _TOOL_RESULT and _payload(event).get("is_error")
        )
        tool_seconds += _union_seconds(_tool_windows(ordered))

        ttft = _time_to_first_output(ordered, first_attempt_start.get(job_id))
        if ttft is not None:
            ttft_samples.append(ttft)

        for event in ordered:
            totals.add(_usage_of(event))
            if (
                event.get("event_type") == _MESSAGE
                and str(_payload(event).get("text") or "").strip()
            ):
                produced_text = True

    model_seconds = max(0.0, wall_seconds - tool_seconds)
    output_tokens = totals.output_tokens
    if output_tokens == 0 and produced_text:
        # A count we can prove wrong is worse than no count. Runtimes report
        # their own usage, and a CLI talking to a model whose response does
        # not carry usage in the shape it reads reports zero rather than
        # nothing — which is what happens to every gateway-served model we
        # actually run. The thread plainly produced assistant text, so zero
        # output tokens is false on its face, and the honest surface is
        # silence until the gateway's ledger backs this figure (CHAT-5, #57),
        # which is the billing authority for it anyway.
        output_tokens = None

    return ThreadStats(
        turns=len(job_ids),
        tool_calls=tool_calls,
        failed_tool_calls=failed_tool_calls,
        wall_seconds=wall_seconds,
        tool_seconds=tool_seconds,
        model_seconds=model_seconds,
        ttft_seconds=(sum(ttft_samples) / len(ttft_samples)) if ttft_samples else None,
        input_tokens=totals.input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        cache_hit_ratio=totals.cache_hit_ratio(),
        output_tokens_per_second=_rate(output_tokens, model_seconds),
        tokens_source="runtime" if totals.saw_usage else "unavailable",
    )


class _TokenTotals:
    """Running token sums that stay ``None`` until a runtime reports one."""

    def __init__(self) -> None:
        self.saw_usage = False
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cache_read_tokens: int | None = None
        self.cache_write_tokens: int | None = None

    def add(self, usage: Mapping[str, Any] | None) -> None:
        """Fold one usage mapping in, ignoring the keys it does not carry."""
        if not usage:
            return
        values = (
            ("input_tokens", _first_int(usage, _INPUT_KEYS)),
            ("output_tokens", _first_int(usage, _OUTPUT_KEYS)),
            ("cache_read_tokens", _first_int(usage, _CACHE_READ_KEYS)),
            ("cache_write_tokens", _first_int(usage, _CACHE_WRITE_KEYS)),
        )
        for field, value in values:
            if value is None:
                continue
            self.saw_usage = True
            current = getattr(self, field)
            setattr(self, field, value if current is None else current + value)

    def cache_hit_ratio(self) -> float | None:
        """Cached share of every input token this thread paid for."""
        if self.cache_read_tokens is None:
            return None
        billed = (self.input_tokens or 0) + (self.cache_write_tokens or 0)
        total = self.cache_read_tokens + billed
        if total <= 0:
            return None
        return self.cache_read_tokens / total


def _earliest_attempt_start_per_job(
    attempts: Iterable[Mapping[str, Any]],
) -> dict[str, datetime]:
    """Map each job to when its first attempt began.

    The *first* attempt, not the last: time-to-first-token should describe
    what the user waited through, and a retried turn made them wait from the
    beginning.
    """
    starts: dict[str, datetime] = {}
    for attempt in attempts:
        job_id = str(attempt.get("job_id"))
        started_at = attempt.get("started_at")
        if not isinstance(started_at, datetime):
            continue
        current = starts.get(job_id)
        if current is None or started_at < current:
            starts[job_id] = started_at
    return starts


def _tool_windows(ordered: Sequence[Mapping[str, Any]]) -> list[tuple[datetime, datetime]]:
    """Pair each ``tool_use`` with its ``tool_result`` and return the spans.

    The pairing key is uniform across every Tier-1 runtime: a call announces
    ``payload.id`` and its result echoes ``payload.tool_use_id``. A call
    without a result (the turn crashed, or is still running) contributes no
    window rather than an open-ended one.
    """
    opened: dict[str, datetime] = {}
    windows: list[tuple[datetime, datetime]] = []
    for event in ordered:
        payload = _payload(event)
        timestamp = _timestamp(event)
        if timestamp is None:
            continue
        if event.get("event_type") == _TOOL_USE:
            call_id = payload.get("id")
            if call_id is not None:
                opened[str(call_id)] = timestamp
        elif event.get("event_type") == _TOOL_RESULT:
            call_id = payload.get("tool_use_id")
            started = opened.pop(str(call_id), None) if call_id is not None else None
            if started is not None and timestamp >= started:
                windows.append((started, timestamp))
    return windows


def _union_seconds(windows: Sequence[tuple[datetime, datetime]]) -> float:
    """Total seconds covered by the windows, counting overlaps once.

    Parallel tool calls are real, and summing their durations would report
    more tool time than the turn took. The union keeps the split between
    tool time and model time interpretable.
    """
    if not windows:
        return 0.0
    total = 0.0
    current_start, current_end = min(windows, key=lambda window: window[0])
    for start, end in sorted(windows, key=lambda window: window[0]):
        if start > current_end:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    total += (current_end - current_start).total_seconds()
    return total


def _time_to_first_output(
    ordered: Sequence[Mapping[str, Any]],
    attempt_started_at: datetime | None,
) -> float | None:
    """Seconds from execution starting to the model's first output."""
    if attempt_started_at is None:
        return None
    for event in ordered:
        if event.get("event_type") not in _FIRST_OUTPUT_KINDS:
            continue
        timestamp = _timestamp(event)
        if timestamp is None:
            continue
        delta = (timestamp - attempt_started_at).total_seconds()
        # A negative delta means the two clocks disagree, which makes this
        # sample meaningless rather than fast.
        return delta if delta >= 0 else None
    return None


def _usage_of(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the usage mapping an event carries, if it carries one.

    Three runtimes put it in three places: claude attaches it to whichever
    assistant event it produced, pi emits a dedicated ``usage`` event, and
    codex hangs it off the terminal ``lifecycle`` record.
    """
    payload = _payload(event)
    event_type = event.get("event_type")
    if event_type == _USAGE:
        nested = payload.get("usage")
        return nested if isinstance(nested, Mapping) else payload
    if event_type in (_MESSAGE, _TOOL_USE, _LIFECYCLE):
        nested = payload.get("usage")
        return nested if isinstance(nested, Mapping) else None
    return None


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _timestamp(event: Mapping[str, Any]) -> datetime | None:
    created_at = event.get("created_at")
    return created_at if isinstance(created_at, datetime) else None


def _event_sort_key(event: Mapping[str, Any]) -> tuple[int, int]:
    """Order events by the store's own sequence, not by wall clock."""
    return (_as_int(event.get("attempt_id")), _as_int(event.get("seq")))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_int(usage: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    """Read the first key present with a non-negative integer value."""
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0 and value.is_integer():
            return int(value)
    return None


def _rate(amount: int | None, seconds: float) -> float | None:
    """Per-second rate, or ``None`` when either side is missing."""
    if amount is None or seconds <= 0:
        return None
    return amount / seconds
