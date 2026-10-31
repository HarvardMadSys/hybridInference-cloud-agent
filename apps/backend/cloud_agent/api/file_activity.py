"""A conversation's File Activity: one read for the Context view's section.

Both routes are the signed-in owner's own and never cached. They answer 404 for
a conversation that is not the caller's or is deleted, the same as its Context
view, and a cursor from another list is a 422 before any SQL runs.

Nothing is derived when a turn is sent, settles, is cancelled or forked: the
read happens when the section asks for a page, and it is bounded — a capped
scan says so on its coverage rather than passing for the whole history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.file_activity.models import (
    DEFAULT_FILE_PAGE,
    DEFAULT_OPERATION_PAGE,
    FILTERS,
    MAX_FILE_PAGE,
    MAX_OPERATION_PAGE,
    MAX_SEARCH_CHARS,
    SORTS,
    FileActivityView,
    FileOperationsPage,
    FilterKey,
    SortKey,
)

if TYPE_CHECKING:
    from cloud_agent.ports.file_activity import FileActivityQueries


class _FileActivityStorage(Protocol):
    """The capability these reads reach for; every call names the owner."""

    file_activity_queries: FileActivityQueries


def _no_store(response: Response) -> None:
    """An owner's activity must not outlive the read."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/v1/agent", tags=["file-activity"], dependencies=[Depends(_no_store)])


def _not_found(job_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
    )


def _invalid(message: str) -> HTTPException:
    return HTTPException(
        status_code=422, detail={"error": {"type": "invalid_filter", "message": message}}
    )


class FileActivityFilterEcho(BaseModel):
    """The filters a page was read with, echoed so a cursor can be bound."""

    model_config = ConfigDict(extra="forbid")

    sort: SortKey
    filter: FilterKey
    q: str = Field(max_length=MAX_SEARCH_CHARS)


@router.get(
    "/jobs/{job_id}/thread/files",
    response_model=FileActivityView,
    response_model_exclude_none=False,
)
async def read_file_activity(
    job_id: str,
    sort: SortKey = Query("active", description="active, lines, latest, path or name"),
    filter: FilterKey = Query("all", description="all, read, written, searched or images"),
    q: str = Query(
        "",
        max_length=MAX_SEARCH_CHARS,
        description="A case-insensitive substring of a file path.",
    ),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(DEFAULT_FILE_PAGE, ge=1, le=MAX_FILE_PAGE),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _FileActivityStorage | None = Depends(get_storage),
) -> FileActivityView:
    """One page of the caller's conversation's File Activity.

    Counts, totals and filter counts cover every operation the bounded scan
    read; the page is one slice of the files matching the filter and search.

    Raises:
        HTTPException: 404 when the conversation is not the caller's or is
            deleted; 422 for a cursor from another list.
    """
    if sort not in SORTS or filter not in FILTERS:
        raise _invalid("Unknown sort or filter.")
    file_store: _FileActivityStorage = require_store(store)
    try:
        view = await file_store.file_activity_queries.file_activity(
            job_id=job_id,
            user_id=user["user_id"],
            sort=sort,
            filter_key=filter,
            q=q.strip(),
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise _invalid(str(exc)) from exc
    if view is None:
        raise _not_found(job_id)
    return view


@router.get(
    "/jobs/{job_id}/thread/files/operations",
    response_model=FileOperationsPage,
)
async def read_file_operations(
    job_id: str,
    path: str = Query(..., min_length=1, max_length=4096),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(DEFAULT_OPERATION_PAGE, ge=1, le=MAX_OPERATION_PAGE),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _FileActivityStorage | None = Depends(get_storage),
) -> FileOperationsPage:
    """One page of one file's recorded operations, newest first.

    ``path`` is a normalized path the overview returned; it is matched as a
    string and never resolved on a filesystem.

    Raises:
        HTTPException: 404 when the conversation is not the caller's or is
            deleted; 422 for a cursor from another list.
    """
    file_store: _FileActivityStorage = require_store(store)
    try:
        page = await file_store.file_activity_queries.file_operations(
            job_id=job_id,
            user_id=user["user_id"],
            path=path,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise _invalid(str(exc)) from exc
    if page is None:
        raise _not_found(job_id)
    return page
