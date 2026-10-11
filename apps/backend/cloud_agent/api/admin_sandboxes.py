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
from cloud_agent.broker_routing import BrokerUrlRefused, broker_for_host
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository, SandboxRepository
    from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries


class _AdminSandboxesStorage(Protocol):
    """The purpose-built inventory capability this route reaches for."""

    admin_sandboxes: AdminSandboxQueries
    sandbox: SandboxRepository
    host_pool: HostPoolRepository


async def _refresh_terminal_activity(
    detail: AdminSandboxDetail, storage: _AdminSandboxesStorage
) -> str | None:
    """Read a fenced broker census before trusting heartbeat terminal data.

    Older runners omit terminal counts even though their broker knows them.
    Older brokers count only the origin member, so even a fresh zero must be
    checked against the group's activity flag before closing admission.
    Keep this recovery independent of the optional Docker resource collector.
    A failed probe leaves terminal activity unknown and stopping disabled.
    """
    sandbox = detail.sandbox
    host = await storage.host_pool.get_host(host=sandbox.host)
    try:
        broker = None if host is None else broker_for_host(host)
    except BrokerUrlRefused:
        broker = None
    if broker is None:
        return "Terminal activity is unavailable. Check the host's workspace broker connection."
    try:
        rows = await broker.list_sandboxes(timeout=5)
    except WorkspaceBrokerError:
        return "The host's workspace broker is unavailable. Restore its connection and refresh details."
    row = next((row for row in rows if row.get("sandbox_id") == sandbox.id), None)
    if (
        row is None
        or row.get("container_id") != sandbox.container_id
        or row.get("generation") != sandbox.generation
        or row.get("origin_thread_id", row.get("thread_id")) != sandbox.origin_thread_id
        or row.get("running") is not True
        or row.get("shared_live") is not True
    ):
        return "The host's container changed. Refresh details before stopping it."
    count = row.get("terminal_count")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 4096:
        return "The host cannot report terminal activity. Update its workspace broker and refresh details."
    # Older brokers count only the origin member's terminals. Their group
    # activity flag still prevents a sibling's terminal from looking idle.
    if count == 0 and row.get("group_busy") is not False:
        return "The host reports active sandbox work. Wait until it is idle and refresh details."
    recorded = await storage.sandbox.record_terminal_observation(
        sandbox_id=sandbox.id,
        host=sandbox.host,
        generation=sandbox.generation,
        container_id=sandbox.container_id or "",
        terminal_count=count,
    )
    if not recorded:
        return "The host's container changed. Refresh details before stopping it."
    return None


def _terminal_activity_needs_refresh(detail: AdminSandboxDetail) -> bool:
    """Recheck idle counts and use the stop command's database freshness clock."""
    sandbox = detail.sandbox
    observed = sandbox.terminal_count_observed_at
    return sandbox.state == "running" and (
        sandbox.terminal_count is None
        or sandbox.terminal_count == 0
        or observed is None
        or (detail.observed_at - observed).total_seconds() > 90
    )


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
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxDetail:
    """Inspect safe container metadata and its current member identities."""
    detail = await queries.detail(sandbox_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    detail = AdminSandboxDetail.model_validate(detail)
    if _terminal_activity_needs_refresh(detail):
        error = await _refresh_terminal_activity(detail, require_store(store))
        if error is not None:
            detail.terminal_activity_error = error
        else:
            refreshed = await queries.detail(sandbox_id)
            if refreshed is not None:
                detail = AdminSandboxDetail.model_validate(refreshed)
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
    current = await storage.admin_sandboxes.detail(sandbox_id)
    if current is not None:
        reviewed = AdminSandboxDetail.model_validate(current)
        sandbox = reviewed.sandbox
        if (
            sandbox.state == "running"
            and sandbox.generation == payload.generation
            and sandbox.container_id == payload.container_id
            and sandbox.last_activity_at == payload.last_activity_at
        ):
            # A legacy broker can heartbeat a fresh origin-only zero while a
            # sibling has an open terminal. Check the whole group before the
            # database closes admission, retaining every original stop fence.
            error = await _refresh_terminal_activity(reviewed, storage)
            if error is not None:
                raise HTTPException(status_code=409, detail=error)
    result = await storage.sandbox.request_admin_stop(
        sandbox_id=sandbox_id,
        generation=payload.generation,
        container_id=payload.container_id,
        last_activity_at=payload.last_activity_at,
        actor_id=admin,
        admin_ip=get_client_ip(request),
    )
    if result == "activity_unknown":
        # A legacy runner heartbeat can clear the report between review and
        # submission. Probe once more, then repeat every original stop fence.
        current = await storage.admin_sandboxes.detail(sandbox_id)
        if current is not None:
            error = await _refresh_terminal_activity(
                AdminSandboxDetail.model_validate(current), storage
            )
            if error is not None:
                raise HTTPException(status_code=409, detail=error)
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
