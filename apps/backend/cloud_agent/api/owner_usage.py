"""The Usage settings page: an owner's containers, their past use, and model usage.

Every read is the signed-in owner's own and never cached. Model usage is the
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
    MAX_CONTAINER_PAGE,
    MAX_HISTORY_DAYS,
    OwnerContainerCursor,
    OwnerContainerList,
    OwnerResourceHistory,
    OwnerTokenUsage,
    history_window,
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

_DAYS = Query(7, ge=1, le=MAX_HISTORY_DAYS)


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
    days: int = _DAYS,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerResourceHistory:
    """Running time and measured CPU and memory for each of the last ``days`` UTC days."""
    since, until = history_window(days)
    return await _require_store(store).owner_usage.resource_history(
        user["user_id"], buckets=day_buckets(since=since, until=until)
    )


@router.get("/tokens", response_model=OwnerTokenUsage)
async def read_owner_tokens(
    days: int = _DAYS,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerTokenUsage:
    """Model usage and cost of the owner's runs created in the last ``days`` UTC days.

    Raises:
        HTTPException: 422 when the selection exceeds the aggregation's bounds,
            503 when it cannot be read inside them. Neither returns a partial sum.
    """
    since, until = history_window(days)
    generated_at = datetime.now(UTC)
    try:
        cohort, rows = await _require_store(store).admin_usage.selection(
            since=since,
            until=until,
            user_id=user["user_id"],
            model=None,
            runtime=None,
            repo=None,
            max_runs=MAX_USAGE_RUNS,
            max_rows=MAX_USAGE_LEDGER_ROWS,
            max_snapshot_bytes=MAX_USAGE_SNAPSHOT_BYTES,
        )
    except UsageQueryTooLarge:
        raise HTTPException(
            status_code=422, detail="Too many runs to sum. Choose a shorter period."
        ) from None
    except UsageQueryUnavailable:
        logger.warning("agent_owner_usage_read_failed")
        raise HTTPException(
            status_code=503, detail="Usage could not be read right now. Please try again."
        ) from None
    # Resolving stored reports is CPU work that grows with the selection; the
    # event loop also serves heartbeats and streams, as the admin page notes.
    return await asyncio.to_thread(
        _summarize, cohort, rows, since=since, until=until, generated_at=generated_at
    )


def _summarize(
    cohort: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    since: datetime,
    until: datetime,
    generated_at: datetime,
) -> OwnerTokenUsage:
    """Reduce one owner's selection to totals, UTC days and models."""
    ledger_by_job: dict[str, list[dict[str, Any]]] = {row["id"]: [] for row in cohort}
    for row in rows:
        ledger_by_job[row["job_id"]].append(row)
    jobs = [{**row, "ledger": ledger_by_job[row["id"]]} for row in cohort]
    prepared = prepare_jobs(jobs, terminal_states=TERMINAL_STATES)
    return OwnerTokenUsage(
        since=since,
        until=until,
        generated_at=generated_at,
        summary=aggregate_prepared(prepared),
        days=days_breakdown(
            jobs,
            buckets=day_buckets(since=since, until=until),
            terminal_states=TERMINAL_STATES,
            prepared=prepared,
        ),
        by_model=breakdown(
            jobs, dimension="model", terminal_states=TERMINAL_STATES, prepared=prepared
        ),
    )
