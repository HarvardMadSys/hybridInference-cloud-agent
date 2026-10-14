"""Cross-task usage aggregation for administrators.

One read answers "who used how much" for runs created in a window, using only
the Gateway snapshots the Agent already stores. It never reads content, never
queries the Gateway and never refreshes a snapshot; a missing snapshot stays
missing and is visible as coverage rather than as zero usage.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime  # noqa: TC002 — FastAPI resolves this at runtime.

from cloud_agent.admin_usage import (
    MAX_USAGE_LEDGER_ROWS,
    MAX_USAGE_RESPONSE_BYTES,
    MAX_USAGE_RUNS,
    MAX_USAGE_SNAPSHOT_BYTES,
    AdminUsageResponse,
    AdminUsageSelection,
    aggregate_prepared,
    breakdown,
    day_buckets,
    days_breakdown,
    prepare_jobs,
    resolve_usage_window,
)
from cloud_agent.api.deps import get_storage, require_store, verify_admin_access
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage.admin_usage_queries import (
    AdminUsageQueries,
    UsageQueryTooLarge,
    UsageQueryUnavailable,
)

logger = get_logger(__name__)


class _AdminUsageStorage(Protocol):
    """The one projection this module's handlers reach the storage layer for."""

    admin_usage: AdminUsageQueries


def _require_store(store: _AdminUsageStorage | None) -> _AdminUsageStorage:
    """The storage layer, or 503."""
    return require_store(store)


_TOO_LARGE = "usage_query_too_large"
_UNAVAILABLE = "usage_temporarily_unavailable"
#: The complete filter surface of this release. Anything else is refused.
_SUPPORTED_QUERY_KEYS = frozenset({"since", "until", "user_id", "model", "runtime", "repo"})


def _no_store(response: Response) -> None:
    """A cached aggregate would outlive the snapshots it was read from."""
    response.headers["Cache-Control"] = "no-store"


# The guard is on the router, not each route: a route added to this surface
# inherits it, so the next usage read cannot ship without one by omission.
router = APIRouter(
    prefix="/admin/agent/usage",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def get_admin_usage_queries(
    store: _AdminUsageStorage | None = Depends(get_storage),
) -> AdminUsageQueries:
    """Expose only the bounded aggregate projections to this route."""
    return _require_store(store).admin_usage


def _error(status: int, error_type: str, message: str) -> HTTPException:
    """Return the client's existing error envelope without echoing SQL or data."""
    return HTTPException(
        status_code=status,
        detail={"error": {"type": error_type, "message": message, "code": status}},
    )


@router.get("", response_model=AdminUsageResponse)
async def get_admin_usage(
    request: Request,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    user_id: str | None = Query(None, max_length=256),
    model: str | None = Query(None, max_length=256),
    runtime: str | None = Query(None, max_length=128),
    repo: str | None = Query(None, max_length=256),
    queries: AdminUsageQueries = Depends(get_admin_usage_queries),
) -> AdminUsageResponse:
    """Aggregate cached usage for runs created in ``[since, until)``.

    Raises:
        HTTPException: 422 for an unusable window, an unsupported filter or an
            oversized selection, and 503 when the selection cannot be read
            inside its bounds. A refused selection returns no totals at all
            rather than a partial sum.
    """
    # An unsupported filter must fail rather than be dropped: silently ignoring
    # ``host`` would answer with a wider cohort than the one that was asked for.
    unknown = sorted(set(request.query_params) - _SUPPORTED_QUERY_KEYS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unsupported filter: {unknown[0]!r}.")
    for value in (user_id, model, runtime, repo):
        if value == "":
            raise HTTPException(status_code=422, detail="An empty filter is not a filter.")
    try:
        window = resolve_usage_window(since=since, until=until)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    since, until = window
    # One clock reading for the window and for the response, so a client can
    # tell when the cached numbers were read rather than when the ledger was.
    generated_at = datetime.now(UTC)

    try:
        cohort, rows = await queries.selection(
            since=since,
            until=until,
            user_id=user_id,
            model=model,
            runtime=runtime,
            repo=repo,
            max_runs=MAX_USAGE_RUNS,
            max_rows=MAX_USAGE_LEDGER_ROWS,
            max_snapshot_bytes=MAX_USAGE_SNAPSHOT_BYTES,
        )
    except UsageQueryTooLarge:
        raise _error(
            422, _TOO_LARGE, "Narrow the time range or add a user or model filter."
        ) from None
    except UsageQueryUnavailable:
        logger.warning("agent_admin_usage_read_failed")
        raise _error(
            503, _UNAVAILABLE, "Usage could not be read right now. Please try again."
        ) from None

    # Resolving the stored reports is CPU work that grows with the selection:
    # seconds at the published bounds. This process serves every request, host
    # heartbeat and event stream from one event loop, so it runs on a thread.
    response, size = await asyncio.to_thread(
        _summarize,
        cohort,
        rows,
        selection=AdminUsageSelection(
            since=since, until=until, user_id=user_id, model=model, runtime=runtime, repo=repo
        ),
        generated_at=generated_at,
        since=since,
        until=until,
    )
    # The byte bound is on what actually leaves: a selection inside its row and
    # snapshot budgets can still serialize past it when a report carries very
    # wide counters. Refuse the whole response rather than trimming days or
    # groups, which would answer a different question than the one asked.
    if size > MAX_USAGE_RESPONSE_BYTES:
        raise _error(422, _TOO_LARGE, "Narrow the time range or add a user or model filter.")
    return response


def _summarize(
    cohort: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    selection: AdminUsageSelection,
    generated_at: datetime,
    since: datetime,
    until: datetime,
) -> tuple[AdminUsageResponse, int]:
    """Reduce one selection to its response and that response's size in bytes.

    CPU-bound and synchronous: the route runs it on a worker thread.
    """
    ledger_by_job: dict[str, list[dict[str, Any]]] = {row["id"]: [] for row in cohort}
    for row in rows:
        ledger_by_job[row["job_id"]].append(row)
    jobs = [{**row, "ledger": ledger_by_job[row["id"]]} for row in cohort]
    # Resolving a stored report is the expensive part, so every reduction below
    # reuses the summaries resolved here rather than resolving them again. The
    # summary, the days and both breakdowns stay one read of one selection.
    prepared = prepare_jobs(jobs, terminal_states=TERMINAL_STATES)

    response = AdminUsageResponse(
        selection=selection,
        generated_at=generated_at,
        summary=aggregate_prepared(prepared),
        days=days_breakdown(
            jobs,
            buckets=day_buckets(since=since, until=until),
            terminal_states=TERMINAL_STATES,
            prepared=prepared,
        ),
        by_user=breakdown(
            jobs,
            dimension="user_id",
            terminal_states=TERMINAL_STATES,
            prepared=prepared,
        ),
        by_model=breakdown(
            jobs,
            dimension="model",
            terminal_states=TERMINAL_STATES,
            prepared=prepared,
        ),
    )
    return response, len(response.model_dump_json().encode())
