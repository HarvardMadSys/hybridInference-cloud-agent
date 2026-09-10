"""Refreshable inference usage snapshots, independent of attempt execution.

The gateway owns the ledger; this service keeps only bounded totals so a
completed turn remains readable when its grant or gateway is unavailable.
Snapshots are attribution by time window, never proof of final settlement.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime  # noqa: TC003 — Pydantic resolves this at runtime.
from decimal import Decimal, localcontext
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import (
    CORE_METRICS,
    USAGE_METRICS,
    GrantUsageReport,
    GrantUsageTotals,
    GrantUsageUnknownRows,
    GrantUsageWindow,
)
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

REFRESH_POLL_SECONDS = 2.0
REFRESH_CONCURRENCY = 4
REFRESH_TIMEOUT_SECONDS = 15.0
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
    totals: GrantUsageTotals | None = None
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

    totals: GrantUsageTotals | None = None
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


def _sum_reports(reports: list[GrantUsageReport]) -> dict[str, Any]:
    if not reports:
        return {"totals": None, "unknown_rows": None, "estimated_calls": 0}
    totals: dict[str, Any] = {"calls": sum(report.totals.calls for report in reports)}
    for name in USAGE_METRICS:
        known = [getattr(report.totals, name) for report in reports]
        known = [value for value in known if value is not None]
        if name == "spent_usd" and known:
            with localcontext() as context:
                context.prec = 160
                totals[name] = format(sum((Decimal(value) for value in known), Decimal(0)), "f")
        else:
            totals[name] = sum(known) if known else None
    return {
        "totals": GrantUsageTotals(**totals),
        "unknown_rows": GrantUsageUnknownRows(
            **{
                name: sum(getattr(report.unknown_rows, name) for report in reports)
                for name in USAGE_METRICS
            }
        ),
        "estimated_calls": sum(report.estimated_calls for report in reports),
    }


def _status(
    reports: list[GrantUsageReport], *, missing: bool, failed: bool, refreshing: bool
) -> LedgerStatus:
    if not reports:
        return "pending" if refreshing or not failed else "unavailable"
    if missing:
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
    all_queried: list[datetime] = []
    all_scheduled: list[datetime] = []
    summaries: list[AttemptLedgerSummary] = []
    missing_attempts = 0
    any_failed = False
    any_refreshing = False
    for attempt_rows in grouped.values():
        reports: list[GrantUsageReport] = []
        queried: list[datetime] = []
        scheduled: list[datetime] = []
        reasons: list[str] = []
        missing = False
        refreshing = False
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
                    reasons.append("usage_invalid_response")
            if report is None:
                missing = True
                if row.get("id") is None:
                    reasons.append("attribution_unavailable")
            else:
                reports.append(report)
                if row.get("queried_at") is not None:
                    queried.append(row["queried_at"])
        first = attempt_rows[0]
        summaries.append(
            AttemptLedgerSummary(
                attempt_id=first["attempt_id"],
                attempt_no=first["attempt_no"],
                status=_status(
                    reports, missing=missing, failed=bool(reasons), refreshing=refreshing
                ),
                reason=reasons[0] if reasons else ("pending" if not reports else None),
                windows=[report.window for report in reports],
                queried_at=min(queried) if queried else None,
                refreshing=refreshing,
                next_refresh_at=min(scheduled) if scheduled else None,
                **_sum_reports(reports),
            )
        )
        all_reports.extend(reports)
        all_queried.extend(queried)
        all_scheduled.extend(scheduled)
        missing_attempts += int(missing)
        any_failed |= bool(reasons)
        any_refreshing |= refreshing
    return JobLedgerSummary(
        status=_status(
            all_reports,
            missing=bool(missing_attempts),
            failed=any_failed,
            refreshing=any_refreshing,
        ),
        attempts=sorted(summaries, key=lambda attempt: attempt.attempt_no),
        missing_attempts=missing_attempts,
        queried_at=min(all_queried) if all_queried else None,
        refreshing=any_refreshing,
        next_refresh_at=min(all_scheduled) if all_scheduled else None,
        **_sum_reports(all_reports),
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
            or any(attempt.reason not in (None, "pending") for attempt in summary.attempts)
            or (
                unknown
                and any(getattr(unknown, name) for name in USAGE_METRICS if name != "reasoning")
            )
        ),
        refreshing=summary.refreshing,
        next_refresh_at=summary.next_refresh_at,
        active=any(
            row.get("job_state") in {"queued", "waiting", "preparing", "running", "publishing"}
            for row in rows
        ),
    )


async def refresh_ledger_entry(store: Any, grants: Any, row: dict[str, Any]) -> None:
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
                timeout=REFRESH_TIMEOUT_SECONDS,
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


async def collect_usage_snapshots(store: Any, grants: Any) -> None:
    """Run ledger reads on their own bounded pool, separate from grant renewal."""
    while True:
        try:
            rows = []
            for _ in range(REFRESH_CONCURRENCY):
                row = await store.claim_due_ledger_refresh()
                if row is None:
                    break
                rows.append(row)
            if rows:
                outcomes = await asyncio.gather(
                    *(refresh_ledger_entry(store, grants, row) for row in rows),
                    return_exceptions=True,
                )
                if any(isinstance(outcome, Exception) for outcome in outcomes):
                    logger.warning("agent_ledger_snapshot_save_failed")
                continue
        except Exception:
            logger.warning("agent_ledger_snapshot_pass_failed")
        await asyncio.sleep(REFRESH_POLL_SECONDS)
