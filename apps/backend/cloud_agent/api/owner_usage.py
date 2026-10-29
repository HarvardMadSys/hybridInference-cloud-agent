"""The Usage settings page: an owner's containers, their past use, and model usage.

Every read is the signed-in owner's own and never cached. A history covers the
last ``days`` UTC days, or with ``all=true`` every whole day since the owner's
first run that one read can sum. Model usage is the
admin Usage aggregation selected for this owner's runs: the same stored
Gateway snapshots summed the same way, so the two pages cannot disagree.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_usage import (
    MAX_USAGE_LEDGER_ROWS,
    MAX_USAGE_RESPONSE_BYTES,
    MAX_USAGE_RUNS,
    MAX_USAGE_SNAPSHOT_BYTES,
    aggregate_prepared,
    breakdown,
    day_buckets,
    days_breakdown,
    prepare_jobs,
)
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.logging_utils import get_logger
from cloud_agent.owner_usage import (
    CONTAINER_CURSOR_SCOPE,
    DEFAULT_CONTAINER_PAGE,
    DEFAULT_HISTORY_DAYS,
    MAX_CONTAINER_PAGE,
    MAX_HISTORY_DAYS,
    OwnerContainerCursor,
    OwnerContainerList,
    OwnerHistoryPeriod,
    OwnerResourceHistory,
    OwnerTokenUsage,
    all_time_window,
    days_period,
)
from cloud_agent.storage.admin_usage_queries import (
    AdminUsageQueries,
    UsageQueryTooLarge,
    UsageQueryUnavailable,
)

if TYPE_CHECKING:
    from cloud_agent.ports import OwnerUsageQueries

logger = get_logger(__name__)


class _OwnerUsageStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    ``admin_usage`` is the bounded usage projection, not an admin authority:
    every call here passes the owner's own ``user_id`` as its filter.
    """

    owner_usage: OwnerUsageQueries
    admin_usage: AdminUsageQueries


def _require_store(store: _OwnerUsageStorage | None) -> _OwnerUsageStorage:
    """This surface's capabilities, or 503 when no database is configured."""
    return require_store(store)


def _no_store(response: Response) -> None:
    """Current use and an owner's history must not outlive the read."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/v1/agent/usage",
    tags=["usage"],
    dependencies=[Depends(_no_store)],
)

_DAYS = Query(None, ge=1, le=MAX_HISTORY_DAYS)
#: ``all`` is a Python builtin, so the parameter has another name in the code.
_ALL = Query(None, alias="all")

_PERIOD_CONFLICT = "usage_period_conflict"
_TOO_LARGE = "usage_query_too_large"
_RESPONSE_TOO_LARGE = "usage_response_too_large"
_UNAVAILABLE = "usage_temporarily_unavailable"


def _error(status: int, error_type: str, message: str) -> HTTPException:
    """The client's error envelope: its ``type`` is what a page translates."""
    return HTTPException(
        status_code=status,
        detail={"error": {"type": error_type, "message": message, "code": status}},
    )


def _unavailable() -> HTTPException:
    logger.warning("agent_owner_usage_read_failed")
    return _error(503, _UNAVAILABLE, "Usage could not be read right now. Please try again.")


async def _period(
    store: _OwnerUsageStorage, user_id: str, *, days: int | None, all_time: bool | None
) -> OwnerHistoryPeriod:
    """The period one history read covers: the last ``days``, or all time.

    Both halves of the page resolve it the same way, so the container history
    and the model usage beside it describe the same days.

    Raises:
        HTTPException: 422 when both ``days`` and ``all`` are given, 503 when
            the owner's runs cannot be read to bound an all-time period.
    """
    if days is not None and all_time is not None:
        raise _error(422, _PERIOD_CONFLICT, "Choose either a number of days or all time.")
    if not all_time:
        return days_period(days or DEFAULT_HISTORY_DAYS)
    now = datetime.now(UTC)
    try:
        earliest, overflow = await store.admin_usage.owner_run_bounds(
            user_id, until=now, max_runs=MAX_USAGE_RUNS
        )
    except UsageQueryUnavailable:
        raise _unavailable() from None
    return all_time_window(earliest=earliest, overflow=overflow, now=now)


@router.get("/containers", response_model=OwnerContainerList)
async def list_owner_containers(
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(DEFAULT_CONTAINER_PAGE, ge=1, le=MAX_CONTAINER_PAGE),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerContainerList:
    """The owner's containers, active first, with counts and current use over all."""
    try:
        after = decode_cursor(cursor, OwnerContainerCursor, CONTAINER_CURSOR_SCOPE)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _require_store(store).owner_usage.containers(
        user["user_id"], cursor=after, limit=limit
    )


@router.get("/resources", response_model=OwnerResourceHistory)
async def read_owner_resources(
    days: int | None = _DAYS,
    all_time: bool | None = _ALL,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerResourceHistory:
    """Running time and measured CPU and memory for each UTC day of the period."""
    owner = _require_store(store)
    period = await _period(owner, user["user_id"], days=days, all_time=all_time)
    history = await owner.owner_usage.resource_history(
        user["user_id"], buckets=day_buckets(since=period.since, until=period.until)
    )
    return history.model_copy(update={"period": period})


@router.get("/tokens", response_model=OwnerTokenUsage)
async def read_owner_tokens(
    days: int | None = _DAYS,
    all_time: bool | None = _ALL,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerTokenUsage:
    """Model usage and cost of the owner's runs created in the period.

    Raises:
        HTTPException: 422 when the selection or its response exceeds the
            aggregation's bounds, 503 when it cannot be read inside them.
            Neither returns a partial sum.
    """
    owner = _require_store(store)
    period = await _period(owner, user["user_id"], days=days, all_time=all_time)
    generated_at = datetime.now(UTC)
    try:
        cohort, rows = await owner.admin_usage.selection(
            since=period.since,
            until=period.until,
            user_id=user["user_id"],
            model=None,
            runtime=None,
            repo=None,
            max_runs=MAX_USAGE_RUNS,
            max_rows=MAX_USAGE_LEDGER_ROWS,
            max_snapshot_bytes=MAX_USAGE_SNAPSHOT_BYTES,
        )
    except UsageQueryTooLarge:
        raise _error(422, _TOO_LARGE, "Too many runs to sum. Choose a shorter period.") from None
    except UsageQueryUnavailable:
        raise _unavailable() from None
    # Resolving stored reports is CPU work that grows with the selection; the
    # event loop also serves heartbeats and streams, as the admin page notes.
    usage, size = await asyncio.to_thread(
        _summarize, cohort, rows, period=period, generated_at=generated_at
    )
    # The admin page's bound on what actually leaves: wide counters can carry a
    # response past it inside every row budget. Refused whole, never trimmed.
    if size > MAX_USAGE_RESPONSE_BYTES:
        raise _error(
            422,
            _RESPONSE_TOO_LARGE,
            "This period's usage is too large to send. Choose a shorter period.",
        )
    return usage


def _summarize(
    cohort: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    period: OwnerHistoryPeriod,
    generated_at: datetime,
) -> tuple[OwnerTokenUsage, int]:
    """Reduce one owner's selection to totals, UTC days and models, and its size in bytes."""
    ledger_by_job: dict[str, list[dict[str, Any]]] = {row["id"]: [] for row in cohort}
    for row in rows:
        ledger_by_job[row["job_id"]].append(row)
    jobs = [{**row, "ledger": ledger_by_job[row["id"]]} for row in cohort]
    prepared = prepare_jobs(jobs, terminal_states=TERMINAL_STATES)
    usage = OwnerTokenUsage(
        since=period.since,
        until=period.until,
        generated_at=generated_at,
        period=period,
        summary=aggregate_prepared(prepared),
        days=days_breakdown(
            jobs,
            buckets=day_buckets(since=period.since, until=period.until),
            terminal_states=TERMINAL_STATES,
            prepared=prepared,
        ),
        by_model=breakdown(
            jobs, dimension="model", terminal_states=TERMINAL_STATES, prepared=prepared
        ),
    )
    return usage, len(usage.model_dump_json().encode())
