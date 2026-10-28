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

**A run's performance estimates** (:func:`summarize_session_statistics`) are
receipt-time measurements, not gateway request latency or process execution
time. Each execution contributes its first-to-last event span, minus the
union of completed tool windows, as model/harness time. Retry idle time is
excluded. First output averages attempt-start-to-first-output samples, which
include sandbox, workspace and CLI startup and the wait for a whole message
block, so it is never time to first token: model TTFT is read from the
gateway's request log by :mod:`cloud_agent.model_ttft`. TPS uses deduplicated
runtime output counts and model time from the same measured executions only;
it never divides gateway counts by unrelated event timing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, Field

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
_ERROR = "error"

#: Events that prove the model has started producing this turn's answer.
#: Used for first output; a ``lifecycle`` event does not qualify, because the
#: harness emits those before the model has said anything. Each arrives once its
#: whole block is complete, so its timestamp is not the model's first token.
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
TOKEN_KEY_GROUPS = (_INPUT_KEYS, _OUTPUT_KEYS, _CACHE_READ_KEYS, _CACHE_WRITE_KEYS)

#: Every payload key the summaries here read: a model call's usage and its
#: identity, how a tool call pairs with its result and whether it failed, and
#: the counters a ``usage`` event may carry at its top level. Assistant text is
#: read only for whether it has any, which a usage read sends as ``has_text``.
#: A read that sends these keys and that flag gets the summary the whole
#: payloads give.
SUMMARY_PAYLOAD_KEYS = (
    "usage",
    "usage_id",
    "usage_scope",
    "id",
    "tool_use_id",
    "is_error",
    *(key for group in TOKEN_KEY_GROUPS for key in group),
)


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
    #: Wall time from each execution's first to last event, summed over the
    #: thread's turns and their attempts. The wait between a failed attempt and
    #: its retry belongs to no execution, so it is not counted.
    wall_seconds: float
    #: Union (never the sum) of tool windows, so parallel calls cannot push
    #: tool time past wall time.
    tool_seconds: float
    #: ``wall_seconds`` minus ``tool_seconds`` — the harness and the model,
    #: which this level of measurement cannot separate from each other.
    model_seconds: float
    #: Mean time from a turn's first attempt starting to its first completed
    #: model output event. It includes sandbox, workspace and CLI startup and
    #: the wait for a whole block, so it is not time to first token; see
    #: :mod:`cloud_agent.model_ttft`. ``None`` when no turn has both timestamps.
    first_output_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    #: Cached share of all input tokens, from ``0.0`` to ``1.0``. ``None`` when the
    #: runtime reports no cache counters.
    cache_hit_ratio: float | None
    #: Output tokens per second of model time, over the executions that report
    #: a believable output count: time without a count and a count without time
    #: stay out of both sides. ``None`` without token counts or without elapsed
    #: model time.
    output_tokens_per_second: float | None
    #: ``"runtime"`` when a runtime reported usage, ``"unavailable"`` when
    #: none did. The UI turns this into the tooltip that explains the dashes.
    tokens_source: str
    #: How the timings above were obtained. Only ``"message_events"`` exists
    #: today; per-tool hook markers would add a more precise value without
    #: changing this field's meaning.
    timing_source: str = "message_events"


#: Stored attempt rollups name their reducer. A future semantic change bumps
#: this value so an unchanged event stream is recomputed instead of serving a
#: result written by an older reducer.
ATTEMPT_ROLLUP_VERSION = 1


@dataclass(frozen=True)
class AttemptStatsRollup:
    """The additive and ordering facts one attempt contributes to a thread.

    This is deliberately not the public summary. Cache-hit ratios, first-output means,
    false zero suppression and token rates all need their numerators and
    denominators retained until every attempt in the thread is combined.
    """

    attempt_id: int
    job_id: str
    tool_calls: int
    failed_tool_calls: int
    wall_seconds: float
    tool_seconds: float
    first_output_at: datetime | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    saw_usage: bool
    produced_text: bool
    rate_tokens: int
    rate_seconds: float


class AttemptStatsAccumulator:
    """Reduce one attempt from already ordered, bounded event pages.

    Event rows themselves never survive add(). The retained state is
    only what can cross a page boundary semantically: open tool calls,
    completed tool intervals, and provisional usage identities. Callers
    must add one attempt's events in ledger sequence order.
    """

    def __init__(self, *, attempt_id: int, job_id: str) -> None:
        self.attempt_id = attempt_id
        self.job_id = job_id
        self.tool_calls = 0
        self.failed_tool_calls = 0
        self.first_output_at: datetime | None = None
        self.produced_text = False
        self._timestamp_count = 0
        self._first_timestamp: datetime | None = None
        self._last_timestamp: datetime | None = None
        self._opened_tools: dict[str, datetime] = {}
        self._tool_windows: list[tuple[datetime, datetime]] = []
        self._provisional_usage = _TokenTotals()
        self._usage_by_response: dict[str, dict[str, int]] = {}
        self._attempt_usage: dict[str, int] | None = None

    def add(self, event: Mapping[str, Any]) -> None:
        """Fold one next-sequence event into the attempt state."""
        event_type = event.get("event_type")
        payload = _payload(event)
        timestamp = _timestamp(event)
        self._record_timestamp(event_type, timestamp)
        self._record_tool(event_type, payload, timestamp)

        if event_type == _MESSAGE and (
            payload.get("has_text") is True or bool(str(payload.get("text") or "").strip())
        ):
            self.produced_text = True
        self._record_usage(event)

    def _record_timestamp(self, event_type: Any, timestamp: datetime | None) -> None:
        """Retain sequence-first span and first-output timing."""
        if timestamp is not None:
            if self._first_timestamp is None:
                self._first_timestamp = timestamp
            self._last_timestamp = timestamp
            self._timestamp_count += 1
            if self.first_output_at is None and event_type in _FIRST_OUTPUT_KINDS:
                self.first_output_at = timestamp

    def _record_tool(
        self, event_type: Any, payload: Mapping[str, Any], timestamp: datetime | None
    ) -> None:
        """Retain counts and pairing state for one tool event."""
        if event_type == _TOOL_USE:
            self.tool_calls += 1
            call_id = payload.get("id")
            if timestamp is not None and call_id is not None:
                self._opened_tools[str(call_id)] = timestamp
        elif event_type == _TOOL_RESULT:
            if payload.get("is_error"):
                self.failed_tool_calls += 1
            call_id = payload.get("tool_use_id")
            started = (
                self._opened_tools.pop(str(call_id), None)
                if timestamp is not None and call_id is not None
                else None
            )
            if started is not None and timestamp is not None and timestamp >= started:
                self._tool_windows.append((started, timestamp))

    def _record_usage(self, event: Mapping[str, Any]) -> None:
        """Retain the canonical totals or provisional response maxima."""
        record = _normalized_usage(event)
        if record is None:
            return
        usage_payload, usage = record
        if usage_payload.get("usage_scope") == "attempt":
            # The last completed attempt total replaces every provisional
            # response counter, exactly as _distinct_usage does.
            self._attempt_usage = usage
            self._provisional_usage = _TokenTotals()
            self._usage_by_response.clear()
            return
        if self._attempt_usage is not None:
            return
        response_id = usage_payload.get("usage_id")
        if not isinstance(response_id, str) or not response_id:
            self._provisional_usage.add(usage)
            return
        current = self._usage_by_response.setdefault(response_id, {})
        for key, value in usage.items():
            current[key] = max(current.get(key, 0), value)

    def extend(self, events: Iterable[Mapping[str, Any]]) -> None:
        """Fold an ordered page without retaining it."""
        for event in events:
            self.add(event)

    def finish(self) -> AttemptStatsRollup:
        """Return the exact version-one rollup for all events added so far."""
        totals = _TokenTotals()
        if self._attempt_usage is not None:
            totals.add(self._attempt_usage)
        else:
            totals.add(
                {
                    key: value
                    for key, value in (
                        ("input_tokens", self._provisional_usage.input_tokens),
                        ("output_tokens", self._provisional_usage.output_tokens),
                        ("cache_read_input_tokens", self._provisional_usage.cache_read_tokens),
                        ("cache_creation_input_tokens", self._provisional_usage.cache_write_tokens),
                    )
                    if value is not None
                }
            )
            for usage in self._usage_by_response.values():
                totals.add(usage)
        span = (
            (self._last_timestamp - self._first_timestamp).total_seconds()
            if self._timestamp_count >= 2
            and self._first_timestamp is not None
            and self._last_timestamp is not None
            else 0.0
        )
        in_tools = _union_seconds(self._tool_windows)
        output = totals.output_tokens
        model = max(0.0, span - in_tools)
        contributes_rate = (
            output is not None and model > 0 and not (output == 0 and self.produced_text)
        )
        return AttemptStatsRollup(
            attempt_id=self.attempt_id,
            job_id=self.job_id,
            tool_calls=self.tool_calls,
            failed_tool_calls=self.failed_tool_calls,
            wall_seconds=span,
            tool_seconds=in_tools,
            first_output_at=self.first_output_at,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            cache_write_tokens=totals.cache_write_tokens,
            saw_usage=totals.saw_usage,
            produced_text=self.produced_text,
            rate_tokens=output if contributes_rate and output is not None else 0,
            rate_seconds=model if contributes_rate else 0.0,
        )


def summarize_attempt_rollup(
    *,
    attempt_id: int,
    job_id: str,
    events: Iterable[Mapping[str, Any]],
) -> AttemptStatsRollup:
    """Reduce one attempt while retaining the facts a thread aggregate needs."""
    accumulator = AttemptStatsAccumulator(attempt_id=attempt_id, job_id=job_id)
    accumulator.extend(sorted(events, key=_event_sort_key))
    return accumulator.finish()


def summarize_thread_rollups(
    *,
    job_ids: Sequence[str],
    attempts: Iterable[Mapping[str, Any]],
    rollups: Iterable[AttemptStatsRollup],
) -> ThreadStats:
    """Combine cached attempt facts with the exact public thread semantics."""
    by_job: dict[str, list[AttemptStatsRollup]] = {job_id: [] for job_id in job_ids}
    for rollup in rollups:
        by_job.setdefault(rollup.job_id, []).append(rollup)
    first_attempt_start = _earliest_attempt_start_per_job(attempts)
    totals = _TokenTotals()
    tool_calls = failed_tool_calls = rate_tokens = 0
    wall_seconds = tool_seconds = rate_seconds = 0.0
    produced_text = False
    first_output_samples: list[float] = []

    for job_id, job_rollups in by_job.items():
        ordered = sorted(job_rollups, key=lambda item: item.attempt_id)
        for item in ordered:
            # Keep the legacy reducer's per-execution addition order. Besides
            # semantics, this preserves the exact IEEE-754 result stored and
            # returned before rollups existed.
            tool_calls += item.tool_calls
            failed_tool_calls += item.failed_tool_calls
            wall_seconds += item.wall_seconds
            tool_seconds += item.tool_seconds
            rate_tokens += item.rate_tokens
            rate_seconds += item.rate_seconds
            produced_text = produced_text or item.produced_text
            totals.add(_rollup_usage(item))
        started_at = first_attempt_start.get(job_id)
        first_output = next(
            (item.first_output_at for item in ordered if item.first_output_at is not None), None
        )
        if started_at is not None and first_output is not None:
            delta = (first_output - started_at).total_seconds()
            if delta >= 0:
                first_output_samples.append(delta)

    output_tokens = totals.output_tokens
    if output_tokens == 0 and produced_text:
        output_tokens = None
    model_seconds = max(0.0, wall_seconds - tool_seconds)
    return ThreadStats(
        turns=len(job_ids),
        tool_calls=tool_calls,
        failed_tool_calls=failed_tool_calls,
        wall_seconds=wall_seconds,
        tool_seconds=tool_seconds,
        model_seconds=model_seconds,
        first_output_seconds=(
            (sum(first_output_samples) / len(first_output_samples))
            if first_output_samples
            else None
        ),
        input_tokens=totals.input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        cache_hit_ratio=totals.cache_hit_ratio(),
        output_tokens_per_second=(
            _rate(rate_tokens, rate_seconds) if output_tokens is not None else None
        ),
        tokens_source="runtime" if totals.saw_usage else "unavailable",
    )


def attempt_rollup_payload(rollup: AttemptStatsRollup) -> dict[str, Any]:
    """Encode one rollup as versioned cache data, excluding relational ids."""
    return {
        "tool_calls": rollup.tool_calls,
        "failed_tool_calls": rollup.failed_tool_calls,
        "wall_seconds": rollup.wall_seconds,
        "tool_seconds": rollup.tool_seconds,
        "first_output_at": (
            rollup.first_output_at.isoformat() if rollup.first_output_at is not None else None
        ),
        "input_tokens": rollup.input_tokens,
        "output_tokens": rollup.output_tokens,
        "cache_read_tokens": rollup.cache_read_tokens,
        "cache_write_tokens": rollup.cache_write_tokens,
        "saw_usage": rollup.saw_usage,
        "produced_text": rollup.produced_text,
        "rate_tokens": rollup.rate_tokens,
        "rate_seconds": rollup.rate_seconds,
    }


def attempt_rollup_from_payload(
    *, attempt_id: int, job_id: str, payload: Mapping[str, Any]
) -> AttemptStatsRollup:
    """Decode trusted versioned cache data; malformed rows fail closed."""
    first_output = payload.get("first_output_at")
    if first_output is not None and not isinstance(first_output, str):
        raise ValueError("invalid first output timestamp")
    first_output_at = datetime.fromisoformat(first_output) if first_output is not None else None
    if first_output_at is not None and first_output_at.tzinfo is None:
        raise ValueError("cached first output timestamp has no timezone")
    return AttemptStatsRollup(
        attempt_id=attempt_id,
        job_id=job_id,
        tool_calls=_cache_int(payload, "tool_calls"),
        failed_tool_calls=_cache_int(payload, "failed_tool_calls"),
        # The legacy reducer follows ledger sequence rather than wall-clock
        # order. Old clock skew can therefore produce a negative span, and
        # the cache must retain it rather than rescan that attempt forever.
        wall_seconds=_cache_float(payload, "wall_seconds", allow_negative=True),
        tool_seconds=_cache_float(payload, "tool_seconds"),
        first_output_at=first_output_at,
        input_tokens=_cache_optional_int(payload, "input_tokens"),
        output_tokens=_cache_optional_int(payload, "output_tokens"),
        cache_read_tokens=_cache_optional_int(payload, "cache_read_tokens"),
        cache_write_tokens=_cache_optional_int(payload, "cache_write_tokens"),
        saw_usage=_cache_bool(payload, "saw_usage"),
        produced_text=_cache_bool(payload, "produced_text"),
        rate_tokens=_cache_int(payload, "rate_tokens"),
        rate_seconds=_cache_float(payload, "rate_seconds"),
    )


def summarize_thread(
    *,
    job_ids: Sequence[str],
    attempts: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> ThreadStats:
    """Summarize one thread from its jobs, attempts and events.

    :param job_ids: every job (turn) in the thread.
    :param attempts: attempt rows carrying ``job_id`` and ``started_at``;
        used only for first output, which needs the moment execution began
        rather than the moment the job was queued.
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
    rate_tokens = 0
    rate_seconds = 0.0
    first_output_samples: list[float] = []
    totals = _TokenTotals()
    produced_text = False

    for job_id, job_events in by_job.items():
        ordered = sorted(job_events, key=_event_sort_key)
        if not ordered:
            continue

        tool_calls += sum(1 for event in ordered if event.get("event_type") == _TOOL_USE)
        failed_tool_calls += sum(
            1
            for event in ordered
            if event.get("event_type") == _TOOL_RESULT and _payload(event).get("is_error")
        )

        # Timed per execution -- one attempt of the turn -- and summed, as the
        # run detail's session statistics are. The wait between a failed
        # attempt and its retry is queueing and backoff, not model time, and a
        # retry's process reuses tool-call ids, so a call the previous process
        # never finished must not pair with the retry's result.
        for execution in _executions(ordered):
            timestamps = [ts for ts in map(_timestamp, execution) if ts is not None]
            span = (timestamps[-1] - timestamps[0]).total_seconds() if len(timestamps) >= 2 else 0.0
            in_tools = _union_seconds(_tool_windows(execution))
            wall_seconds += span
            tool_seconds += in_tools
            counted = _TokenTotals()
            for usage in _distinct_usage(execution):
                totals.add(usage)
                counted.add(usage)
            output = counted.output_tokens
            model = max(0.0, span - in_tools)
            if output is not None and model > 0 and not (output == 0 and _has_text(execution)):
                # Only an execution with both measurements feeds the rate: its
                # count over its own model time, never another attempt's.
                rate_tokens += output
                rate_seconds += model

        first_output = _time_to_first_output(ordered, first_attempt_start.get(job_id))
        if first_output is not None:
            first_output_samples.append(first_output)

        produced_text = produced_text or _has_text(ordered)

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
        first_output_seconds=(
            (sum(first_output_samples) / len(first_output_samples))
            if first_output_samples
            else None
        ),
        input_tokens=totals.input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        cache_hit_ratio=totals.cache_hit_ratio(),
        output_tokens_per_second=(
            _rate(rate_tokens, rate_seconds) if output_tokens is not None else None
        ),
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


def normalized_runtime_input_tokens(
    *, runtime: str | None, events: Iterable[Mapping[str, Any]]
) -> int | None:
    """Return cache-inclusive input only when the runtime's counters establish it.

    Claude and pi report uncached input, cache reads and cache writes as
    disjoint counters. Codex input already includes cache hits. Missing cache
    counters cannot be assumed to be zero, and unknown harnesses have no
    verified input convention. Keep the original counters alongside this
    nullable projection for diagnostics.
    """
    if runtime not in {"claude-code", "pi", "codex"}:
        return None
    usages = list(_distinct_usage(sorted(events, key=_event_sort_key)))
    if not usages:
        return None
    total = 0
    for usage in usages:
        keys = (
            (_INPUT_KEYS, _CACHE_READ_KEYS, _CACHE_WRITE_KEYS)
            if runtime in {"claude-code", "pi"}
            else (_INPUT_KEYS,)
        )
        values = [_first_int(usage, group) for group in keys]
        if any(value is None for value in values):
            return None
        total += sum(value for value in values if value is not None)
    return total


def _distinct_usage(events: Sequence[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    """Prefer completed attempt totals; otherwise count each response once.

    Claude's block events repeat a response's usage and may update output
    counters later. Its result carries the completed query total. Identity
    is scoped to each job's attempt, so retries and follow-ups remain additive.
    Older events without identity retain their original additive semantics.
    """
    by_attempt: dict[Any, list[tuple[Mapping[str, Any], dict[str, int]]]] = {}
    for event in events:
        record = _normalized_usage(event)
        if record is None:
            continue
        by_attempt.setdefault(event.get("attempt_id"), []).append(record)

    for records in by_attempt.values():
        final = next(
            (
                usage
                for payload, usage in reversed(records)
                if payload.get("usage_scope") == "attempt"
            ),
            None,
        )
        if final is not None:
            yield final
            continue
        by_response: dict[str, dict[str, int]] = {}
        for payload, usage in records:
            response_id = payload.get("usage_id")
            if not isinstance(response_id, str) or not response_id:
                yield usage
                continue
            current = by_response.setdefault(response_id, {})
            for key, value in usage.items():
                current[key] = max(current.get(key, 0), value)
        yield from by_response.values()


def _normalized_usage(
    event: Mapping[str, Any],
) -> tuple[Mapping[str, Any], dict[str, int]] | None:
    """Return one compact usage record in the reducer's canonical key shape."""
    usage = _usage_of(event)
    if not usage:
        return None
    normalized = {
        keys[0]: value
        for keys in TOKEN_KEY_GROUPS
        if (value := _first_int(usage, keys)) is not None
    }
    return (_payload(event), normalized) if normalized else None


def _earliest_attempt_start_per_job(
    attempts: Iterable[Mapping[str, Any]],
) -> dict[str, datetime]:
    """Map each job to when its first attempt began.

    The *first* attempt, not the last: first output should describe what the
    user waited through, and a retried turn made them wait from the beginning.
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


def _executions(ordered: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Split one turn's ordered events into its attempts, each still in order.

    An attempt is one execution: its own process, its own span of time and its
    own tool-call ids. The events arrive sorted by attempt and then sequence,
    so grouping keeps each execution's own order.
    """
    executions: dict[Any, list[Mapping[str, Any]]] = {}
    for event in ordered:
        executions.setdefault(event.get("attempt_id"), []).append(event)
    return list(executions.values())


def _has_text(events: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any of these events is assistant text the user could see."""
    return any(
        event.get("event_type") == _MESSAGE
        and (
            _payload(event).get("has_text") is True
            or bool(str(_payload(event).get("text") or "").strip())
        )
        for event in events
    )


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
    first_output = _first_output_at(ordered)
    if first_output is not None:
        delta = (first_output - attempt_started_at).total_seconds()
        # A negative delta means the two clocks disagree, which makes this
        # sample meaningless rather than fast.
        return delta if delta >= 0 else None
    return None


def _first_output_at(ordered: Sequence[Mapping[str, Any]]) -> datetime | None:
    """Return the first sequenced model-output timestamp that is usable."""
    return next(
        (
            timestamp
            for event in ordered
            if event.get("event_type") in _FIRST_OUTPUT_KINDS
            if (timestamp := _timestamp(event)) is not None
        ),
        None,
    )


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
    if event_type in (_MESSAGE, _THINKING, _TOOL_USE, _LIFECYCLE, _ERROR):
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


def _rollup_usage(rollup: AttemptStatsRollup) -> dict[str, int]:
    """Put retained counters back in the canonical shape ``_TokenTotals`` reads."""
    values = {
        "input_tokens": rollup.input_tokens,
        "output_tokens": rollup.output_tokens,
        "cache_read_input_tokens": rollup.cache_read_tokens,
        "cache_creation_input_tokens": rollup.cache_write_tokens,
    }
    return {key: value for key, value in values.items() if value is not None}


def _cache_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid cached {key}")
    return value


def _cache_optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return None if value is None else _cache_int(payload, key)


def _cache_float(payload: Mapping[str, Any], key: str, *, allow_negative: bool = False) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid cached {key}")
    try:
        decoded = float(value)
    except OverflowError as exc:
        raise ValueError(f"invalid cached {key}") from exc
    if not isfinite(decoded) or (decoded < 0 and not allow_negative):
        raise ValueError(f"invalid cached {key}")
    return decoded


def _cache_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"invalid cached {key}")
    return value


def _rate(amount: int | None, seconds: float) -> float | None:
    """Per-second rate, or ``None`` when either side is missing."""
    if amount is None or seconds <= 0:
        return None
    return amount / seconds


class SessionStatistics(BaseModel):
    """Run performance estimates, with unavailable measurements left absent."""

    model_seconds: float | None = Field(None, ge=0)
    tool_seconds: float | None = Field(None, ge=0)
    #: Event-derived attempt start to first completed output, startup included;
    #: not time to first token, which the run page reads from the gateway.
    first_output_seconds: float | None = Field(None, ge=0)
    output_tokens_per_second: float | None = Field(None, ge=0)
    timing_source: Literal["message_events"] = "message_events"


def summarize_session_statistics(
    *,
    job_ids: Sequence[str],
    attempts: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> SessionStatistics:
    """Summarize a run without pairing events or charging idle time across retries.

    Total times require valid event spans and complete tool pairs in every
    execution. Missing timing is not zero, and an unfinished tool window is
    not model time. First output and TPS retain only the executions with their
    required measurements; their missing samples never enter a denominator as
    zero.
    """
    allowed_jobs = set(job_ids)
    by_attempt: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    attempt_rows: dict[tuple[str, Any], Mapping[str, Any]] = {}
    for candidate in attempts:
        job_id = str(candidate.get("job_id"))
        if job_id not in allowed_jobs or candidate.get("attempt_id") is None:
            continue
        key = (job_id, candidate["attempt_id"])
        attempt_rows[key] = candidate
        by_attempt[key] = []

    missing_identity = False
    for event in events:
        job_id = str(event.get("job_id"))
        if job_id not in allowed_jobs:
            continue
        if event.get("attempt_id") is None:
            missing_identity = True
            continue
        by_attempt.setdefault((job_id, event["attempt_id"]), []).append(event)

    complete_timing = bool(by_attempt) and not missing_identity
    model_seconds = tool_seconds = rate_seconds = 0.0
    rate_tokens = 0
    first_output_samples: list[float] = []
    for key, execution_events in by_attempt.items():
        ordered = sorted(execution_events, key=_event_sort_key)
        attempt = attempt_rows.get(key)
        stats = summarize_thread(
            job_ids=[key[0]],
            attempts=[attempt] if attempt is not None else [],
            events=ordered,
        )
        if stats.first_output_seconds is not None:
            first_output_samples.append(stats.first_output_seconds)
        if not _has_complete_timing(ordered):
            complete_timing = False
            continue
        model_seconds += stats.model_seconds
        tool_seconds += stats.tool_seconds
        if stats.output_tokens is not None and stats.model_seconds > 0:
            rate_tokens += stats.output_tokens
            rate_seconds += stats.model_seconds

    return SessionStatistics(
        model_seconds=model_seconds if complete_timing else None,
        tool_seconds=tool_seconds if complete_timing else None,
        first_output_seconds=(
            sum(first_output_samples) / len(first_output_samples) if first_output_samples else None
        ),
        output_tokens_per_second=rate_tokens / rate_seconds if rate_seconds > 0 else None,
    )


def _has_complete_timing(ordered: Sequence[Mapping[str, Any]]) -> bool:
    """Require an observed event span and unambiguous, fully paired tool windows."""
    observed = [event.get("created_at") for event in ordered]
    if len(observed) < 2 or any(not isinstance(ts, datetime) for ts in observed):
        return False
    timestamps = [ts for ts in observed if isinstance(ts, datetime)]
    if timestamps[-1] <= timestamps[0] or any(right < left for left, right in pairwise(timestamps)):
        return False
    if not any(event.get("event_type") in _FIRST_OUTPUT_KINDS for event in ordered):
        return False

    opened: set[str] = set()
    for event in ordered:
        kind = event.get("event_type")
        if kind not in {"tool_use", "tool_result"}:
            continue
        call_id = _payload(event).get("id" if kind == "tool_use" else "tool_use_id")
        if not isinstance(call_id, str) or not call_id:
            return False
        if kind == "tool_use":
            if call_id in opened:
                return False
            opened.add(call_id)
        elif call_id not in opened:
            return False
        else:
            opened.remove(call_id)
    return not opened
