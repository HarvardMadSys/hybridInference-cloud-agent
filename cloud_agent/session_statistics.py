"""Execution-scoped performance estimates from content-free runtime events.

These are receipt-time measurements, not gateway request latency or process
execution time. Each execution contributes its first-to-last event span,
minus the union of completed tool windows, as model/harness time. Retry idle
time is excluded. TTFT averages attempt-start-to-first-output samples. TPS
uses deduplicated runtime output counts and model time from the same measured
executions only; it never divides gateway counts by unrelated event timing.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from cloud_agent.thread_stats import (
    _FIRST_OUTPUT_KINDS,
    _event_sort_key,
    _payload,
    summarize_thread,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


class SessionStatistics(BaseModel):
    """Run performance estimates, with unavailable measurements left absent."""

    model_seconds: float | None = Field(None, ge=0)
    tool_seconds: float | None = Field(None, ge=0)
    ttft_seconds: float | None = Field(None, ge=0)
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
    not model time. TTFT and TPS retain only the executions with their required
    measurements; their missing samples never enter a denominator as zero.
    """
    allowed_jobs = set(job_ids)
    by_attempt: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    attempt_rows: dict[tuple[str, Any], Mapping[str, Any]] = {}
    for attempt in attempts:
        job_id = str(attempt.get("job_id"))
        if job_id not in allowed_jobs or attempt.get("attempt_id") is None:
            continue
        key = (job_id, attempt["attempt_id"])
        attempt_rows[key] = attempt
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
    ttft_samples: list[float] = []
    for key, execution_events in by_attempt.items():
        ordered = sorted(execution_events, key=_event_sort_key)
        attempt = attempt_rows.get(key)
        stats = summarize_thread(
            job_ids=[key[0]],
            attempts=[attempt] if attempt is not None else [],
            events=ordered,
        )
        if stats.ttft_seconds is not None:
            ttft_samples.append(stats.ttft_seconds)
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
        ttft_seconds=sum(ttft_samples) / len(ttft_samples) if ttft_samples else None,
        output_tokens_per_second=rate_tokens / rate_seconds if rate_seconds > 0 else None,
    )


def _has_complete_timing(ordered: Sequence[Mapping[str, Any]]) -> bool:
    """Require an observed event span and unambiguous, fully paired tool windows."""
    timestamps = [event.get("created_at") for event in ordered]
    if len(timestamps) < 2 or any(not isinstance(ts, datetime) for ts in timestamps):
        return False
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
