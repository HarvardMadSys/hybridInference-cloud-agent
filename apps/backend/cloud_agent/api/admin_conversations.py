"""Admin-only conversation navigation over existing execution observations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import AwareDatetime  # noqa: TC002 — FastAPI resolves annotations at runtime.

from cloud_agent.admin_conversations import (
    AdminConversationDetail,
    AdminConversationList,
    ConversationCursor,
    ConversationTurnsCursor,
)
from cloud_agent.admin_observability import (
    RunFilters,
    cursor_scope,
    decode_cursor,
    resolve_run_window,
)
from cloud_agent.api.deps import get_storage, require_store, verify_admin_access
from cloud_agent.storage.admin_conversation_queries import turns_scope

if TYPE_CHECKING:
    from cloud_agent.storage.admin_conversation_queries import AdminConversationQueries


class _Storage(Protocol):
    admin_conversations: AdminConversationQueries


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/agent/conversations", dependencies=[Depends(_no_store)])


def get_admin_conversation_queries(
    store: _Storage | None = Depends(get_storage),
) -> AdminConversationQueries:
    """Expose only the admin metadata reader to these routes."""
    return require_store(store).admin_conversations


@router.get("", response_model=AdminConversationList)
async def list_admin_conversations(
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    filters: RunFilters = Depends(),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(50, ge=1, le=200),
    _admin: str = Depends(verify_admin_access),
    queries: AdminConversationQueries = Depends(get_admin_conversation_queries),
) -> AdminConversationList:
    """List distinct conversations in a bounded turn-creation cohort."""
    try:
        parsed = decode_cursor(
            cursor, ConversationCursor, cursor_scope(["conversations", filters.model_dump()])
        )
        since, until = resolve_run_window(since=since, until=until, cursor=parsed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await queries.list_conversations(
        filters=filters,
        since=since,
        until=until,
        cursor=parsed,
        limit=limit,
    )


@router.get("/{thread_id}", response_model=AdminConversationDetail)
async def get_admin_conversation(
    thread_id: str = Path(max_length=256),
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    filters: RunFilters = Depends(),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(50, ge=1, le=200),
    _admin: str = Depends(verify_admin_access),
    queries: AdminConversationQueries = Depends(get_admin_conversation_queries),
) -> AdminConversationDetail:
    """Read a conversation's full turn history with an optional match context."""
    try:
        if (since is None) != (until is None):
            raise ValueError("Provide both ends of the time window.")
        if since is not None:
            since, until = resolve_run_window(since=since, until=until, cursor=None)
        parsed = decode_cursor(
            cursor, ConversationTurnsCursor, turns_scope(thread_id, filters, since, until)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    detail = await queries.detail(
        thread_id, filters=filters, since=since, until=until, cursor=parsed, limit=limit
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return detail
