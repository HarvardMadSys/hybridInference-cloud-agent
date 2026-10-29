"""The Usage settings page: an owner's containers, their past use, and model usage.

Every read is the signed-in owner's own and never cached. Model usage is the
admin Usage aggregation selected for this owner's runs: the same stored
Gateway snapshots summed the same way, so the two pages cannot disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_usage import MAX_USAGE_RESPONSE_BYTES
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
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
)
from cloud_agent.storage.admin_usage_queries import (
    AdminUsageQueries,
    UsageQueryTooLarge,
    UsageQueryUnavailable,
)
from cloud_agent.usage_batches import owner_usage
from cloud_agent.usage_range import RangePreset, UsageRange, buckets, day_range, resolve_range

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


class _UsageRangeStorage(Protocol):
    """What resolving a range reads: the owner's first record, for ``all``."""

    owner_usage: OwnerUsageQueries


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

_DAYS = Query(
    None,
    ge=1,
    le=MAX_HISTORY_DAYS,
    description="The last N UTC days, today included. Prefer `range`.",
)
RANGE_QUERY = Query(
    None,
    alias="range",
    description=(
        "today, 7d, 30d or all, in UTC days. `all` starts at the owner's earliest "
        "retained run or container and is never narrowed."
    ),
)


async def resolve_period(
    store: _UsageRangeStorage, user_id: str, preset: RangePreset | None, days: int | None
) -> UsageRange:
    """Resolve the one range every half of the page reads.

    Raises:
        HTTPException: 422 when both ``range`` and ``days`` are given.
    """
    if preset is not None and days is not None:
        raise HTTPException(
            status_code=422,
            detail={"error": {"type": "invalid_range", "message": "Give range or days, not both."}},
        )
    if preset is None:
        return day_range(days or 7, max_days=MAX_HISTORY_DAYS)
    first = await store.owner_usage.first_record_at(user_id) if preset == "all" else None
    return resolve_range(preset, first_record_at=first)


def _refusal(status: int, kind: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"type": kind, "message": message}})


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
    period: RangePreset | None = RANGE_QUERY,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerResourceHistory:
    """Running time and measured CPU and memory per UTC bucket of the range."""
    usage_store = _require_store(store)
    resolved = await resolve_period(usage_store, user["user_id"], period, days)
    history = await usage_store.owner_usage.resource_history(
        user["user_id"], buckets=buckets(resolved.since, resolved.until, resolved.bucket)
    )
    history.period = resolved
    return history


@router.get("/tokens", response_model=OwnerTokenUsage)
async def read_owner_tokens(
    days: int | None = _DAYS,
    period: RangePreset | None = RANGE_QUERY,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _OwnerUsageStorage | None = Depends(get_storage),
) -> OwnerTokenUsage:
    """Model usage and cost of the owner's runs created in the range.

    A range larger than one bounded selection is summed in bounded batches
    (:mod:`cloud_agent.usage_batches`), never narrowed.

    Raises:
        HTTPException: 422 when even a minimal batch exceeds the aggregation's
            bounds or the response would, 503 when it cannot be read in time.
            Neither returns a partial sum.
    """
    usage_store = _require_store(store)
    resolved = await resolve_period(usage_store, user["user_id"], period, days)
    generated_at = datetime.now(UTC)
    try:
        usage = await owner_usage(
            usage_store.admin_usage,
            user_id=user["user_id"],
            since=resolved.since,
            until=resolved.until,
            size=resolved.bucket,
        )
    except UsageQueryTooLarge:
        raise _refusal(422, "usage_too_large", "This usage is too large to sum.") from None
    except UsageQueryUnavailable:
        logger.warning("agent_owner_usage_read_failed")
        raise _refusal(
            503, "usage_unavailable", "Usage could not be read right now. Please try again."
        ) from None
    result = OwnerTokenUsage(
        since=resolved.since,
        until=resolved.until,
        generated_at=generated_at,
        summary=usage.summary,
        days=usage.buckets,
        by_model=usage.by_model,
        period=resolved,
        batches=usage.batches,
    )
    if len(result.model_dump_json().encode()) > MAX_USAGE_RESPONSE_BYTES:
        raise _refusal(422, "usage_too_large", "This usage is too large to return.")
    return result
