"""Admin inventory of running retained sandbox containers across all owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_sandboxes import (
    AdminSandboxDetail,
    AdminSandboxList,
    AdminSandboxStopRequest,
    SandboxCursor,
    SandboxStateFilter,
    sandbox_cursor_scope,
)
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    require_store,
    verify_admin_access,
)

if TYPE_CHECKING:
    from cloud_agent.ports import SandboxRepository
    from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries


class _AdminSandboxesStorage(Protocol):
    """The purpose-built inventory capability this route reaches for."""

    admin_sandboxes: AdminSandboxQueries
    sandbox: SandboxRepository


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
    state: SandboxStateFilter = Query("running"),
    queries: AdminSandboxQueries = Depends(get_admin_sandbox_queries),
) -> AdminSandboxList:
    """List lifecycle metadata, defaulting to retained running containers."""
    try:
        parsed = decode_cursor(cursor, SandboxCursor, sandbox_cursor_scope(state))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if state == "running":
        return await queries.list_running(cursor=parsed, limit=limit)
    return await queries.list_sandboxes(cursor=parsed, limit=limit, state=state)


@router.get("/{sandbox_id}", response_model=AdminSandboxDetail)
async def get_admin_sandbox(
    sandbox_id: str,
    queries: AdminSandboxQueries = Depends(get_admin_sandbox_queries),
) -> AdminSandboxDetail:
    """Inspect safe container metadata and its current member identities."""
    detail = await queries.detail(sandbox_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    return detail


@router.post("/{sandbox_id}/stop", response_model=AdminSandboxDetail, status_code=202)
async def stop_admin_sandbox(
    sandbox_id: str,
    payload: AdminSandboxStopRequest,
    request: Request,
    admin: str = Depends(verify_admin_access),
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxDetail:
    """Request a fenced idle stop; the owning host confirms it asynchronously."""
    storage = require_store(store)
    result = await storage.sandbox.request_admin_stop(
        sandbox_id=sandbox_id,
        generation=payload.generation,
        container_id=payload.container_id,
        last_activity_at=payload.last_activity_at,
        actor_id=admin,
        admin_ip=get_client_ip(request),
    )
    errors = {
        "not_found": (404, "Sandbox not found."),
        "stale": (409, "Sandbox activity or container changed. Refresh before stopping it."),
        "not_running": (409, "Only a running sandbox can be stopped."),
        "busy": (409, "Sandbox has an active turn, operation, or snapshot. Wait until it is idle."),
        "terminals_open": (409, "Sandbox has open terminals. Close them before stopping it."),
        "activity_unknown": (
            409,
            "Current terminal activity is unavailable. Refresh and try again.",
        ),
    }
    if result != "accepted":
        status, message = errors[result]
        raise HTTPException(status_code=status, detail=message)
    detail = await storage.admin_sandboxes.detail(sandbox_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    return detail
