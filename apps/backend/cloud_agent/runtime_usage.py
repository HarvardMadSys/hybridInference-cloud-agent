"""What a turn's agent reported about its own model use, ready to be summed.

A turn runs on its owner's model connection, and that provider keeps the API
key, the quota and the bill: this service records no cost and does not count
model requests. What it has is the agent's own token report, reduced per
attempt into the thread-statistics rollups. This module turns one turn's
rollups into the totals the usage pages add up:

- **input** (``tokens_in``) counts every input token, cached or not, and only
  where the agent's counters establish that sum. Claude Code and pi report
  uncached input, cache reads and cache writes as disjoint counters, so the
  three are added; Codex input already includes cache hits. Any other agent's
  convention is not verified, so its input stays unknown rather than guessed.
- **output**, **cache reads** and **cache writes** are as reported.
- **reasoning** and **cost** are never in an agent's report: they stay unknown,
  and each reporting attempt counts as a row that lacked them.

An attempt that reported nothing is coverage, never zero usage.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves the field types at runtime.
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from cloud_agent.grant_usage import USAGE_METRICS, GrantUsageUnknownRows, UsageSums

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Every number in a summary is the agent's own report.
RUNTIME_USAGE_SOURCE = "runtime_reported"
#: Why an attempt has no usage: its agent reported none.
NO_RUNTIME_REPORT = "not_reported_by_agent"
#: Agents whose input counter excludes cache reads and writes.
_DISJOINT_INPUT = frozenset({"claude-code", "pi"})
#: Agents whose input counter already includes cache reads.
_INCLUSIVE_INPUT = frozenset({"codex"})

RuntimeUsageStatus = Literal["pending", "available", "partial", "unavailable"]


class RuntimeAttemptUsage(BaseModel):
    """One attempt's share of a turn's usage."""

    attempt_id: int
    attempt_no: int
    status: RuntimeUsageStatus
    reason: str | None = None


class RuntimeJobUsage(BaseModel):
    """One turn's runtime-reported usage across every attempt it made."""

    source: Literal["runtime_reported"] = RUNTIME_USAGE_SOURCE
    status: RuntimeUsageStatus
    totals: UsageSums | None = None
    unknown_rows: GrantUsageUnknownRows | None = None
    #: Agents report no estimate flag: nothing is counted as estimated.
    estimated_calls: int = 0
    queried_at: datetime | None = None
    refreshing: bool = False
    missing_attempts: int = 0
    attempts: list[RuntimeAttemptUsage] = Field(default_factory=list)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def cache_inclusive_input(runtime: str | None, attempt: Mapping[str, Any]) -> int | None:
    """Every input token of one attempt, where the agent's counters establish it."""
    uncached = _count(attempt.get("input_tokens"))
    if runtime in _INCLUSIVE_INPUT:
        return uncached
    if runtime in _DISJOINT_INPUT:
        parts = (
            uncached,
            _count(attempt.get("cache_read_tokens")),
            _count(attempt.get("cache_write_tokens")),
        )
        if any(part is None for part in parts):
            return None
        return sum(part for part in parts if part is not None)
    return None


def _attempt_metrics(runtime: str | None, attempt: Mapping[str, Any]) -> dict[str, int | None]:
    return {
        "tokens_in": cache_inclusive_input(runtime, attempt),
        "tokens_out": _count(attempt.get("output_tokens")),
        "cache_read": _count(attempt.get("cache_read_tokens")),
        "cache_write": _count(attempt.get("cache_write_tokens")),
        "reasoning": None,
        "spent_usd": None,
    }


def summarize_runtime_job(job: Mapping[str, Any], *, terminal: bool) -> RuntimeJobUsage:
    """Reduce one turn's attempt rollups to its runtime-reported usage.

    ``job["usage"]`` holds the turn's attempts, each with ``attempt_id``,
    ``attempt_no``, ``saw_usage`` and the rollup's token counters. A turn still
    running waits for its report; a finished one without any is unavailable.
    """
    runtime = job.get("runtime")
    attempts: list[RuntimeAttemptUsage] = []
    reported: list[dict[str, int | None]] = []
    for attempt in sorted(job.get("usage") or [], key=lambda row: int(row["attempt_no"])):
        if attempt.get("saw_usage"):
            reported.append(_attempt_metrics(runtime, attempt))
            state: RuntimeUsageStatus = "available"
            reason = None
        else:
            state = "unavailable" if terminal else "pending"
            reason = NO_RUNTIME_REPORT if terminal else None
        attempts.append(
            RuntimeAttemptUsage(
                attempt_id=int(attempt["attempt_id"]),
                attempt_no=int(attempt["attempt_no"]),
                status=state,
                reason=reason,
            )
        )
    missing = sum(1 for attempt in attempts if attempt.status == "unavailable")
    if not reported:
        return RuntimeJobUsage(
            status="unavailable" if terminal else "pending",
            missing_attempts=missing,
            attempts=attempts,
        )
    sums: dict[str, Any] = {"calls": None}
    unknown: dict[str, int] = {}
    for name in USAGE_METRICS:
        known = [metrics[name] for metrics in reported if metrics[name] is not None]
        sums[name] = sum(value for value in known if value is not None) if known else None
        unknown[name] = len(reported) - len(known)
    return RuntimeJobUsage(
        status="partial" if missing else "available",
        totals=UsageSums(**sums),
        unknown_rows=GrantUsageUnknownRows(**unknown),
        missing_attempts=missing,
        attempts=attempts,
    )


__all__ = [
    "NO_RUNTIME_REPORT",
    "RUNTIME_USAGE_SOURCE",
    "RuntimeAttemptUsage",
    "RuntimeJobUsage",
    "cache_inclusive_input",
    "summarize_runtime_job",
]
