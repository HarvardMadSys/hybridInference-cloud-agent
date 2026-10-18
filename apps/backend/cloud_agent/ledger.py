"""Refreshable inference usage snapshots, independent of attempt execution.

The gateway owns the ledger; this service keeps only bounded totals so a
completed turn remains readable when its grant or gateway is unavailable.
Snapshots are attribution by time window, never proof of final settlement.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import (
    CORE_METRICS,
    DERIVED_AMOUNT_CHARS,
    USAGE_METRICS,
    GrantUsageDerivedTotals,
    GrantUsageReport,
    GrantUsageUnknownRows,
    GrantUsageWindow,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting, number_setting
from cloud_agent.thread_stats import normalized_runtime_input_tokens, summarize_thread
from cloud_agent.workers.background_wake import LEDGER

if TYPE_CHECKING:
    from cloud_agent.ports import LedgerRepository
    from cloud_agent.workers.background_wake import BackgroundWake

logger = get_logger(__name__)

REFRESH_POLL_SECONDS = 2.0
#: Longest an idle collector waits on its wake-up before looking anyway.
LEDGER_IDLE_S = 30.0
#: Shortest wait, so a schedule a moment in the past cannot spin the loop.
REFRESH_MIN_WAIT_S = 0.05
REFRESH_CONCURRENCY = 4
REFRESH_TIMEOUT_SECONDS = 15.0
#: A stored or derived report this service will not add to a total. One reason
#: covers every rejection so the browser and the stored error stay content-free.
INVALID_REPORT = "usage_invalid_response"
#: An attempt that ended before it bound a grant, and whose agent reported no
#: model usage: nothing was called, so there is no usage to wait for or miss.
NO_MODEL_CALLS = "no_model_calls"
#: The width a derived amount may occupy. Reaching it needs more reports than a
#: selection can hold, so it is a guard rather than a limit; keeping it in a
#: module attribute lets its refusal path be tested without impossible inputs.
_derived_amount_chars = DERIVED_AMOUNT_CHARS
_SAFE_ERRORS = frozenset(
    {
        "usage_unsupported_schema",
        "usage_invalid_response",
        "usage_response_mismatch",
        "usage_unavailable",
        "usage_unconfigured",
        "ledger_unavailable",
        "unknown_grant",
        "internal_api_not_configured",
        "invalid_usage_query",
        "binding_changed",
        "refresh_failed",
    }
)

LedgerStatus = Literal["pending", "available", "partial", "unavailable", "stale"]


class LedgerSummary(BaseModel):
    """Known totals accompanied by their missing data and refresh state."""

    status: LedgerStatus
    totals: GrantUsageDerivedTotals | None = None
    unknown_rows: GrantUsageUnknownRows | None = None
    estimated_calls: int = 0
    queried_at: datetime | None = None
    refreshing: bool = False
    next_refresh_at: datetime | None = None


class AttemptLedgerSummary(LedgerSummary):
    """A retry's snapshots, without service credentials or grant identifiers."""

    attempt_id: int
    attempt_no: int
    reason: str | None = None
    windows: list[GrantUsageWindow] = Field(default_factory=list)


class JobLedgerSummary(LedgerSummary):
    """One job's ledger usage across every execution attempt, including failures."""

    source: Literal["gateway_api_logs"] = "gateway_api_logs"
    attribution: Literal["grant_time_window"] = "grant_time_window"
    finalized: Literal[False] = False
    missing_attempts: int = 0
    attempts: list[AttemptLedgerSummary] = Field(default_factory=list)


class RuntimeUsage(BaseModel):
    """Runtime self-reported tokens for the same job and retries."""

    source: Literal["runtime"] = "runtime"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    normalized_input_tokens: int | None = None


class ConversationUsage(BaseModel):
    """Cached usage for the owned conversation, without per-attempt diagnostics."""

    totals: GrantUsageDerivedTotals | None = None
    unknown_rows: GrantUsageUnknownRows | None = None
    estimated_calls: int = 0
    queried_at: datetime | None = None
    turn_count: int = 0
    incomplete: bool = False
    refreshing: bool = False
    next_refresh_at: datetime | None = None
    active: bool = False


class JobUsageResponse(BaseModel):
    """Usage sources and a cached conversation total for an owner's run."""

    ledger: JobLedgerSummary
    runtime: RuntimeUsage
    conversation: ConversationUsage | None = None
    version: str | None = Field(
        default=None,
        description=(
            "Orders two reads of one job's usage, asked for or pushed: the one whose "
            "version compares greater as a string saw everything the other did. Opaque."
        ),
    )


def summarize_job_ledger(ledger_rows: list[dict[str, Any]], *, terminal: bool) -> JobLedgerSummary:
    """Account for one job's snapshots with no runtime and no Gateway read.

    Owner reads, the admin run detail and cross-task aggregation share this so
    that a job means the same thing everywhere. A turn that never claimed has no
    future snapshot to wait for once it is terminal.
    """
    ledger = summarize_ledger(ledger_rows)
    if not ledger_rows and terminal:
        # A turn cancelled before claim has no future snapshot to wait for.
        ledger.status = "unavailable"
    return ledger


def summarize_job_usage(
    *,
    ledger_rows: list[dict[str, Any]],
    runtime_inputs: dict[str, Any],
    runtime: str | None,
    terminal: bool,
    conversation: ConversationUsage | None = None,
) -> JobUsageResponse:
    """Use one accounting definition for owner and admin reads of a turn.

    Callers authorize their own read and supply explicitly projected inputs.
    This function neither fetches content nor queues a Gateway refresh.
    """
    stats = summarize_thread(**runtime_inputs)
    return JobUsageResponse(
        ledger=summarize_job_ledger(ledger_rows, terminal=terminal),
        runtime=RuntimeUsage(
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            cache_read_tokens=stats.cache_read_tokens,
            normalized_input_tokens=normalized_runtime_input_tokens(
                runtime=runtime, events=runtime_inputs["events"]
            ),
        ),
        conversation=conversation,
    )


def _sum_amounts(amounts: list[str]) -> str:
    """Add decimal strings and return the exact sum.

    No rounding happens here, at any scale or magnitude: this is the reducer the
    owner read, the run detail and the cross-task aggregate share, and a total
    that depends on how the gateway happened to split its reports across jobs
    would be wrong. Display rounding belongs to the page that shows the number.

    The sum needs one more significant digit than its widest operand plus the
    carry of adding many of them, so the working precision is sized from the
    operands rather than taken from the default context, which would round.
    """
    integer_digits = 1
    fractional_digits = 0
    for amount in amounts:
        integer, _, fraction = amount.partition(".")
        integer_digits = max(integer_digits, len(integer))
        fractional_digits = max(fractional_digits, len(fraction))
    with localcontext() as context:
        context.prec = len(amounts).bit_length() + integer_digits + fractional_digits + 2
        context.Emax = context.prec
        context.Emin = -context.prec
        total = sum((Decimal(amount) for amount in amounts), Decimal(0))
    return f"{total:f}"


def _sum_reports(
    reports: list[GrantUsageReport], reasons: list[str] | None = None
) -> dict[str, Any] | None:
    """Sum valid reports exactly, or refuse a derived total this service cannot hold.

    Records the refusal in ``reasons`` and returns ``None`` so the caller counts
    the snapshot as unreadable and never reports a rounded or truncated amount.
    """
    if not reports:
        return {"totals": None, "unknown_rows": None, "estimated_calls": 0}
    totals: dict[str, Any] = {"calls": sum(report.totals.calls for report in reports)}
    for name in USAGE_METRICS:
        known = [getattr(report.totals, name) for report in reports]
        known = [value for value in known if value is not None]
        if name == "spent_usd" and known:
            total = _sum_amounts(known)
            if len(total) > _derived_amount_chars:
                if reasons is not None:
                    reasons.append(INVALID_REPORT)
                return None
            totals[name] = total
        else:
            totals[name] = sum(known) if known else None
    return {
        "totals": GrantUsageDerivedTotals(**totals),
        "unknown_rows": GrantUsageUnknownRows(
            **{
                name: sum(getattr(report.unknown_rows, name) for report in reports)
                for name in USAGE_METRICS
            }
        ),
        "estimated_calls": sum(report.estimated_calls for report in reports),
    }


def _status(
    reports: list[GrantUsageReport],
    *,
    missing: bool,
    failed: bool,
    refreshing: bool,
    unreadable: bool = False,
) -> LedgerStatus:
    if not reports:
        return "pending" if refreshing or not failed else "unavailable"
    if missing or unreadable:
        # A snapshot this service cannot add to a total reads as incomplete,
        # never as a smaller number.
        return "partial"
    if failed:
        return "stale"
    # Only a missing core metric degrades the snapshot. Unreported reasoning
    # rows stay visible in ``unknown_rows``; see ``CORE_METRICS``.
    if any(getattr(report.unknown_rows, name) for report in reports for name in CORE_METRICS):
        return "partial"
    return "available"


def summarize_ledger(rows: list[dict[str, Any]]) -> JobLedgerSummary:
    """Sum snapshots without turning missing attempts or metrics into zeroes."""
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["attempt_id"]].append(row)
    all_reports: list[GrantUsageReport] = []
    job_reasons: list[str] = []
    all_queried: list[datetime] = []
    all_scheduled: list[datetime] = []
    summaries: list[AttemptLedgerSummary] = []
    missing_attempts = 0
    unreadable_attempts = 0
    no_call_attempts = 0
    any_failed = False
    any_refreshing = False
    for attempt_rows in grouped.values():
        reports: list[GrantUsageReport] = []
        queried: list[datetime] = []
        scheduled: list[datetime] = []
        reasons: list[str] = []
        missing = False
        refreshing = False
        no_model_calls = False
        for row in attempt_rows:
            refreshing |= bool(row.get("refreshing"))
            if row.get("next_refresh_at") is not None:
                scheduled.append(row["next_refresh_at"])
            error = row.get("last_error_code")
            if error:
                reasons.append(error if error in _SAFE_ERRORS else "refresh_failed")
            report = None
            if row.get("snapshot") is not None:
                try:
                    report = GrantUsageReport.model_validate(row["snapshot"])
                    if report.grant_id != row.get("grant_id"):
                        report = None
                except (ValidationError, ValueError, TypeError):
                    pass
                if report is None:
                    reasons.append(INVALID_REPORT)
            if report is None and row.get("id") is None:
                # No ledger row: the attempt never bound a grant to read.
                status = row.get("attempt_status")
                if status == "running":
                    # Not bound *yet*: it is starting, and its usage pending.
                    pass
                elif status is not None and row.get("model_usage_reported") is False:
                    # It ended before its agent could call a model.
                    no_model_calls = True
                else:
                    missing = True
                    reasons.append("attribution_unavailable")
            elif report is None:
                missing = True
            else:
                reports.append(report)
                if row.get("queried_at") is not None:
                    queried.append(row["queried_at"])
        first = attempt_rows[0]
        attempt_sum = _sum_reports(reports, reasons)
        quiet = no_model_calls and not reports and not reasons
        no_call_attempts += int(quiet)
        summaries.append(
            AttemptLedgerSummary(
                attempt_id=first["attempt_id"],
                attempt_no=first["attempt_no"],
                status="available"
                if quiet
                else _status(
                    reports,
                    missing=missing,
                    failed=bool(reasons),
                    refreshing=refreshing,
                    unreadable=attempt_sum is None,
                ),
                reason=NO_MODEL_CALLS
                if quiet
                else (reasons[0] if reasons else ("pending" if not reports else None)),
                windows=[report.window for report in reports],
                queried_at=min(queried) if queried else None,
                refreshing=refreshing,
                next_refresh_at=min(scheduled) if scheduled else None,
                **(attempt_sum or {"totals": None, "unknown_rows": None, "estimated_calls": 0}),
            )
        )
        all_reports.extend(reports)
        all_queried.extend(queried)
        all_scheduled.extend(scheduled)
        missing_attempts += int(missing)
        unreadable_attempts += int(attempt_sum is None)
        any_failed |= bool(reasons) or attempt_sum is None
        any_refreshing |= refreshing
    job_sum = _sum_reports(all_reports, job_reasons)
    # Every attempt ended before its agent could call a model: there is
    # nothing to wait for, and nothing was spent. "available" with no totals.
    nothing_called = bool(summaries) and no_call_attempts == len(summaries)
    return JobLedgerSummary(
        status="available"
        if nothing_called
        else _status(
            all_reports,
            missing=bool(missing_attempts),
            failed=any_failed,
            refreshing=any_refreshing,
            unreadable=unreadable_attempts > 0 or job_sum is None,
        ),
        attempts=sorted(summaries, key=lambda attempt: attempt.attempt_no),
        missing_attempts=missing_attempts,
        queried_at=min(all_queried) if all_queried else None,
        refreshing=any_refreshing,
        next_refresh_at=min(all_scheduled) if all_scheduled else None,
        **(job_sum or {"totals": None, "unknown_rows": None, "estimated_calls": 0}),
    )


def summarize_conversation(rows: list[dict[str, Any]]) -> ConversationUsage:
    """Sum this conversation's snapshots once, retaining gaps and stale reads."""
    summary = summarize_ledger([row for row in rows if row.get("attempt_id") is not None])
    unknown = summary.unknown_rows
    return ConversationUsage(
        totals=summary.totals,
        unknown_rows=unknown,
        estimated_calls=summary.estimated_calls,
        queried_at=summary.queried_at,
        turn_count=len({row["job_id"] for row in rows}),
        incomplete=bool(
            summary.missing_attempts
            # An attempt that called no model has nothing missing.
            or any(
                attempt.reason not in (None, "pending", NO_MODEL_CALLS)
                for attempt in summary.attempts
            )
            or (
                unknown
                and any(getattr(unknown, name) for name in USAGE_METRICS if name != "reasoning")
            )
        ),
        refreshing=summary.refreshing,
        next_refresh_at=summary.next_refresh_at,
        active=any(
            row.get("job_state") in {"queued", "preparing", "running", "publishing"} for row in rows
        ),
    )


async def refresh_ledger_entry(store: LedgerRepository, grants: Any, row: dict[str, Any]) -> None:
    """Fetch a claimed reference, preserving cached data on every failure."""
    report = None
    error_code = None
    try:
        if row["binding_id"] != grants.binding_id:
            error_code = "binding_changed"
        elif row["window_since"] >= row["window_until"]:
            error_code = "invalid_usage_query"
        else:
            result = await asyncio.wait_for(
                grants.usage(row["grant_id"], since=row["window_since"], until=row["window_until"]),
                timeout=number_setting("ledger_refresh_timeout_s", REFRESH_TIMEOUT_SECONDS),
            )
            report = result.model_dump(mode="json")
    except GatewayError as exc:
        error_code = exc.error_type if exc.error_type in _SAFE_ERRORS else "refresh_failed"
    except TimeoutError:
        error_code = "usage_unavailable"
    except Exception:
        # Error strings can contain HTTP headers or arbitrary upstream data.
        # Store and log only the bounded reason, never the exception message.
        error_code = "refresh_failed"
    await store.complete_ledger_refresh(
        ledger_id=row["id"],
        refresh_generation=row["refresh_generation"],
        terminal=row["terminal"],
        report=report,
        error_code=error_code,
    )


async def collect_usage_snapshots(
    store: LedgerRepository, grants: Any, *, wake: BackgroundWake | None = None
) -> None:
    """Run ledger reads on their own bounded pool, separate from grant renewal.

    Refill each slot as soon as its query completes, so a slow Gateway read
    does not hold up other jobs. With ``wake`` a collector with spare capacity
    waits for an attempt to stop or a refresh to be scheduled, and otherwise
    until the earliest scheduled one is due,
    never longer than ``LEDGER_IDLE_S``. Without it, or while nothing listens,
    it looks every ``REFRESH_POLL_SECONDS``.
    """
    inflight: set[asyncio.Task[None]] = set()
    try:
        while True:
            concurrency = integer_setting("ledger_refresh_concurrency", REFRESH_CONCURRENCY)
            try:
                while len(inflight) < concurrency:
                    row = await store.claim_due_ledger_refresh()
                    if row is None:
                        break
                    inflight.add(asyncio.create_task(refresh_ledger_entry(store, grants, row)))
            except Exception:
                logger.warning("agent_ledger_snapshot_pass_failed")

            waiter: asyncio.Task[Any] | None = None
            waiting: set[asyncio.Task[Any]] = set(inflight)
            if len(inflight) < concurrency:
                # A free slot must also notice newly due work while another
                # query is slow, rather than only waiting for that query.
                idle_s = REFRESH_POLL_SECONDS
                if wake is not None:
                    # While nothing listens, look on the old short clock.
                    idle_s = (
                        number_setting("ledger_idle_s", LEDGER_IDLE_S)
                        if wake.listening
                        else REFRESH_POLL_SECONDS
                    )
                    with contextlib.suppress(Exception):
                        due = await store.next_ledger_refresh_due()
                        if due is not None:
                            idle_s = min(idle_s, (due - datetime.now(UTC)).total_seconds())
                delay = max(idle_s, REFRESH_MIN_WAIT_S)
                waiter = asyncio.create_task(
                    asyncio.sleep(delay) if wake is None else wake.wait(LEDGER, delay)
                )
                waiting.add(waiter)
            try:
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if waiter is not None:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            completed = inflight.intersection(done)
            inflight.difference_update(completed)
            errors = [task.exception() for task in completed if not task.cancelled()]
            if any(error is not None for error in errors):
                logger.warning("agent_ledger_snapshot_save_failed")
    finally:
        for task in inflight:
            task.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)
