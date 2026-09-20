"""Cross-task usage aggregation over stored Gateway snapshots.

One run is one ``agent_jobs`` turn and every execution attempt it made. A
selection is by job creation time, a day bucket is a UTC creation day, and every
number comes from the same per-job accounting the owner and admin run reads use.
Missing snapshots stay missing: this module never converts them into zeroes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.grant_usage import (
    DERIVED_AMOUNT_CHARS,
    USAGE_METRICS,
    GrantUsageDerivedTotals,
    GrantUsageUnknownRows,
)
from cloud_agent.ledger import JobLedgerSummary, summarize_job_ledger

#: Longest selectable window, in whole days.
MAX_USAGE_WINDOW_DAYS = 90
#: Default window when neither boundary is given.
DEFAULT_USAGE_WINDOW_DAYS = 7
#: Jobs in one selection, before the query is refused.
MAX_USAGE_RUNS = 10_000
#: Attempt and ledger rows in one selection, before the query is refused.
MAX_USAGE_LEDGER_ROWS = 50_000
#: Serialized bytes of the projected snapshots in one selection.
MAX_USAGE_SNAPSHOT_BYTES = 32 * 1024 * 1024
#: Serialized bytes of the HTTP JSON one selection may return. Checked on the
#: real serialized response, because no per-row budget bounds a sum of wide
#: counters. A measured worst case with 4,000-digit counters in every metric
#: over 89 day buckets returned 429 KiB, so reaching 1 MiB needs counters no
#: real gateway report carries; the bound is here so that input still fails
#: rather than arriving.
MAX_USAGE_RESPONSE_BYTES = 1024 * 1024
#: Groups shown per breakdown before the remainder is combined.
BREAKDOWN_TOP_GROUPS = 20
#: The grouped model of a job whose configured model is empty or missing.
UNKNOWN_MODEL_GROUP = None

LedgerStatus = Literal["pending", "available", "partial", "unavailable", "stale"]
BreakdownDimension = Literal["user_id", "model"]
DayBucket = tuple[datetime, datetime]


#: Every state one job's ledger summary can end in, each counted exactly once.
_STATUS_ORDER: tuple[LedgerStatus, ...] = (
    "available",
    "partial",
    "stale",
    "pending",
    "unavailable",
)


class AdminUsageTotals(GrantUsageDerivedTotals):
    """Known metric sums for one selection, group or day; unknowns stay null."""


class AdminUsageAggregate(BaseModel):
    """Counts, sums and coverage for one cohort of runs and its executions."""

    model_config = ConfigDict(extra="forbid")

    run_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    totals: AdminUsageTotals | None = None
    unknown_rows: GrantUsageUnknownRows | None = None
    estimated_calls: int = Field(default=0, ge=0)
    oldest_queried_at: datetime | None = None
    runs_with_snapshot: int = Field(default=0, ge=0)
    missing_attempts: int = Field(default=0, ge=0)
    status_counts: dict[LedgerStatus, int] = Field(
        default_factory=lambda: dict.fromkeys(_STATUS_ORDER, 0)
    )
    active_runs: int = Field(default=0, ge=0)
    refreshing_runs: int = Field(default=0, ge=0)


class AdminUsageGroup(BaseModel):
    """One breakdown row; its key is the complete stored identity."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = None
    usage: AdminUsageAggregate


class AdminUsageBreakdown(BaseModel):
    """The largest groups by recorded cost and the remainder as one row."""

    model_config = ConfigDict(extra="forbid")

    group_count: int = Field(ge=0)
    groups: list[AdminUsageGroup]
    other: AdminUsageGroup | None = None


class AdminUsageDay(BaseModel):
    """One UTC creation day that intersects the selected window.

    ``since`` and ``until`` are the intersection with the selection, not an
    interval in which the usage happened: a run created just before ``until``
    counts here in full.
    """

    model_config = ConfigDict(extra="forbid")

    date: str
    since: datetime
    until: datetime
    usage: AdminUsageAggregate


class AdminUsageSelection(BaseModel):
    """The exact cohort and basis behind every number in the response."""

    model_config = ConfigDict(extra="forbid")

    # Overlapping selections recount a run created in both, because the basis is
    # creation time and each run contributes its full cached usage.
    since: datetime
    until: datetime
    time_basis: Literal["job_created_at"] = "job_created_at"
    day_timezone: Literal["UTC"] = "UTC"
    model_basis: Literal["job_configured_model"] = "job_configured_model"
    user_id: str | None = None
    model: str | None = None
    runtime: str | None = None
    repo: str | None = None


class AdminUsageResponse(BaseModel):
    """One read view's totals, UTC day buckets and two independent breakdowns."""

    model_config = ConfigDict(extra="forbid")

    # The breakdowns partition the same runs as ``summary``. Adding the user and
    # model breakdowns together would double count every run.
    schema_version: Literal[1] = 1
    source: Literal["gateway_api_logs"] = "gateway_api_logs"
    attribution: Literal["grant_time_window"] = "grant_time_window"
    finalized: Literal[False] = False
    selection: AdminUsageSelection
    generated_at: datetime
    summary: AdminUsageAggregate
    days: list[AdminUsageDay]
    by_user: AdminUsageBreakdown
    by_model: AdminUsageBreakdown


def resolve_usage_window(
    *,
    since: datetime | None,
    until: datetime | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Complete and bound a creation-time selection, in UTC.

    Args:
        since: Inclusive lower bound; pass ``None`` to default to seven days
            before ``until``.
        until: Exclusive upper bound; pass ``None`` to default to the current
            time.
        now: The single clock reading used when ``until`` is omitted.

    Returns:
        The pair of UTC instants to select ``agent_jobs.created_at`` by. A
        boundary carrying any offset is converted, so an equivalent local time
        selects the same jobs.

    Raises:
        ValueError: If the window is empty or longer than the allowed span.
    """
    until = until or now or datetime.now(UTC)
    since = since or until - timedelta(days=DEFAULT_USAGE_WINDOW_DAYS)
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("Time window boundaries must include a time zone.")
    since, until = since.astimezone(UTC), until.astimezone(UTC)
    if not since < until:
        raise ValueError("Use a time window whose start is before its end.")
    if until - since > timedelta(days=MAX_USAGE_WINDOW_DAYS):
        raise ValueError(f"Use a time window of at most {MAX_USAGE_WINDOW_DAYS} days.")
    return since, until


def _day_start(value: datetime) -> datetime:
    """Return the UTC midnight of the creation day, whatever the offset given."""
    return value.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def day_buckets(*, since: datetime, until: datetime) -> list[DayBucket]:
    """Return every UTC creation day intersecting ``[since, until)``.

    A boundary in the middle of a day still contributes that whole day, so the
    returned windows overlap neither each other nor the selection outside it.
    """
    first = _day_start(since)
    last = _day_start(until - timedelta(microseconds=1))
    buckets: list[DayBucket] = []
    day = first
    while day <= last:
        buckets.append((day, day + timedelta(days=1)))
        day += timedelta(days=1)
    return [(max(start, since), min(end, until)) for start, end in buckets]


def day_key(created_at: datetime) -> str:
    """Name the UTC day a run is counted under, from its creation time."""
    return created_at.astimezone(UTC).date().isoformat()


def _unknown_rows(rows: list[GrantUsageUnknownRows]) -> GrantUsageUnknownRows | None:
    if not rows:
        return None
    return GrantUsageUnknownRows(
        **{name: sum(getattr(row, name) for row in rows) for name in USAGE_METRICS}
    )


def _sum_totals(totals: list[GrantUsageDerivedTotals]) -> AdminUsageTotals | None:
    """Add known values only, leaving each unknown metric unknown.

    Every amount here is an exact decimal string whose width is already bounded
    by the per-job sum, and adding exact decimals cannot round, so the sum of a
    cohort is exact. Only the string length is checked, as a contract guard.
    """
    if not totals:
        return None
    summed: dict[str, Any] = {"calls": sum(total.calls for total in totals)}
    for name in USAGE_METRICS:
        known = [value for value in (getattr(total, name) for total in totals) if value is not None]
        if name != "spent_usd":
            summed[name] = sum(known) if known else None
            continue
        if not known:
            summed[name] = None
            continue
        with localcontext() as context:
            # Adding exact decimals keeps every digit, but the default context
            # would round a total wider than the per-job bound.
            context.prec = 2 * DERIVED_AMOUNT_CHARS
            amount = format(sum((Decimal(value) for value in known), Decimal(0)), "f")
        if len(amount) > DERIVED_AMOUNT_CHARS:
            raise ValueError("a cohort amount exceeds the derived totals contract")
        summed[name] = amount
    return AdminUsageTotals(**summed)


#: One job, ready to be added to any number of cohorts. Resolving a stored
#: report is the expensive part of a selection, so it happens exactly once per
#: job and the four reductions reuse the result.
Prepared = tuple[dict[str, Any], JobLedgerSummary, bool]


class _Cohort:
    """One aggregate under construction.

    The per-job amounts are collected first and added once, at the end. Adding
    them as each job arrives would re-sum the whole cohort for every job, which
    turns a selection into quadratic decimal work.
    """

    def __init__(self) -> None:
        self.usage = AdminUsageAggregate(run_count=0, attempt_count=0)
        self._totals: list[GrantUsageDerivedTotals] = []
        self._unknown: list[GrantUsageUnknownRows] = []

    def add(self, summary: JobLedgerSummary, *, active: bool) -> None:
        """Account for one job exactly once in this cohort."""
        usage = self.usage
        usage.run_count += 1
        usage.attempt_count += len(summary.attempts)
        usage.estimated_calls += summary.estimated_calls
        usage.missing_attempts += summary.missing_attempts
        usage.status_counts[summary.status] = usage.status_counts.get(summary.status, 0) + 1
        usage.refreshing_runs += int(summary.refreshing)
        usage.active_runs += int(active)
        if summary.totals is None:
            # No readable snapshot: this job contributes coverage, never zero.
            return
        usage.runs_with_snapshot += 1
        if summary.queried_at is not None:
            oldest = usage.oldest_queried_at
            usage.oldest_queried_at = (
                summary.queried_at if oldest is None else min(oldest, summary.queried_at)
            )
        self._totals.append(summary.totals)
        if summary.unknown_rows is not None:
            self._unknown.append(summary.unknown_rows)

    def finish(self) -> AdminUsageAggregate:
        """Sum the collected amounts exactly once."""
        self.usage.totals = _sum_totals(self._totals)
        self.usage.unknown_rows = _unknown_rows(self._unknown)
        return self.usage


def prepare_jobs(rows: list[dict[str, Any]], *, terminal_states: tuple[str, ...]) -> list[Prepared]:
    """Resolve each job's ledger summary once, for reuse by every reduction."""
    return [
        (
            row,
            summarize_job_ledger(
                row.get("ledger") or [], terminal=row.get("state") in terminal_states
            ),
            row.get("state") not in terminal_states,
        )
        for row in rows
    ]


def aggregate_prepared(prepared: list[Prepared]) -> AdminUsageAggregate:
    """Account for jobs whose ledger summaries this request already resolved."""
    return _aggregate(prepared)


def _aggregate(prepared: list[Prepared]) -> AdminUsageAggregate:
    cohort = _Cohort()
    for _, summary, active in prepared:
        cohort.add(summary, active=active)
    return cohort.finish()


def aggregate_jobs(
    rows: list[dict[str, Any]], *, terminal_states: tuple[str, ...]
) -> AdminUsageAggregate:
    """Account for a cohort of jobs, each through the shared job ledger summary."""
    return _aggregate(prepare_jobs(rows, terminal_states=terminal_states))


def _key_order(key: str | None) -> tuple[int, bytes]:
    """Order groups by their complete key, with an unknown key last."""
    return (1, b"") if key is None else (0, key.encode("utf-8"))


def _spend_order(usage: AdminUsageAggregate) -> Decimal:
    spent = usage.totals.spent_usd if usage.totals else None
    return Decimal(spent) if spent is not None else Decimal(0)


def _ranked(groups: dict[str | None, AdminUsageAggregate]) -> list[AdminUsageGroup]:
    """Sort groups by recorded cost descending; unknown cost ranks last.

    A group with no readable snapshot has no recorded cost at all. It stays in
    the result so its runs and coverage are visible, and it is never described
    as cheap.
    """
    ranked = sorted(
        groups.items(),
        key=lambda item: (
            item[1].totals is None,
            -_spend_order(item[1]),
            _key_order(item[0]),
        ),
    )
    return [AdminUsageGroup(key=key, usage=usage) for key, usage in ranked]


def breakdown(
    rows: list[dict[str, Any]],
    *,
    dimension: BreakdownDimension,
    terminal_states: tuple[str, ...],
    top: int = BREAKDOWN_TOP_GROUPS,
    prepared: list[Prepared] | None = None,
) -> AdminUsageBreakdown:
    """Group a cohort by one stored job field and rank the groups by cost.

    The remainder is aggregated from its own jobs rather than by subtracting the
    shown groups from the summary, so a group with no readable cost still
    contributes its coverage and statuses. A group whose key is missing or empty
    is kept as one explicit unknown group instead of being guessed at. Pass
    ``prepared`` to reuse summaries this request already resolved.
    """
    entries = prepare_jobs(rows, terminal_states=terminal_states) if prepared is None else prepared
    cohorts: dict[str | None, list[Prepared]] = {}
    for entry in entries:
        cohorts.setdefault(entry[0].get(dimension) or UNKNOWN_MODEL_GROUP, []).append(entry)
    groups = {key: _aggregate(group) for key, group in cohorts.items()}
    ranked = _ranked(groups)
    rest = ranked[top:]
    remainder: list[Prepared] = []
    for group in rest:
        remainder.extend(cohorts[group.key])
    return AdminUsageBreakdown(
        group_count=len(groups),
        groups=ranked[:top],
        other=AdminUsageGroup(key=None, usage=_aggregate(remainder)) if rest else None,
    )


def days_breakdown(
    rows: list[dict[str, Any]],
    *,
    buckets: list[DayBucket],
    terminal_states: tuple[str, ...],
    prepared: list[Prepared] | None = None,
) -> list[AdminUsageDay]:
    """Bucket a cohort by UTC creation day, keeping empty days visible."""
    entries = prepare_jobs(rows, terminal_states=terminal_states) if prepared is None else prepared
    by_day: dict[str, list[Prepared]] = {start.date().isoformat(): [] for start, _ in buckets}
    for entry in entries:
        created = entry[0].get("created_at")
        if created is None:
            continue
        by_day.setdefault(day_key(created), []).append(entry)
    return [
        AdminUsageDay(
            date=day_key(start),
            since=start,
            until=end,
            usage=_aggregate(by_day[day_key(start)]),
        )
        for start, end in buckets
    ]
