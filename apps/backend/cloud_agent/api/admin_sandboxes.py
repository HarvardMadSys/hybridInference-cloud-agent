"""Admin inventory of running retained sandbox containers across all owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_sandboxes import SANDBOX_CURSOR_SCOPE, AdminSandboxList, SandboxCursor
from cloud_agent.api.deps import get_storage, require_store, verify_admin_access

if TYPE_CHECKING:
    from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries


class _AdminSandboxesStorage(Protocol):
    """The purpose-built inventory capability this route reaches for."""

    admin_sandboxes: AdminSandboxQueries


def get_admin_sandbox_queries(
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxQueries:
    """Expose only metadata inventory reads to the admin route."""
    return require_store(store).admin_sandboxes


def _no_store(response: Response) -> None:
    """Keep this live cross-user inventory out of browser and proxy caches."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/sandboxes",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


@router.get("", response_model=AdminSandboxList)
async def list_admin_sandboxes(
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(100, ge=1, le=200),
    queries: AdminSandboxQueries = Depends(get_admin_sandbox_queries),
) -> AdminSandboxList:
    """List running containers, including retained groups between active turns."""
    try:
        parsed = decode_cursor(cursor, SandboxCursor, SANDBOX_CURSOR_SCOPE)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await queries.list_running(cursor=parsed, limit=limit)
