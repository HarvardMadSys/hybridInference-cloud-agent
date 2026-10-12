"""Admin inventory of running retained sandbox containers across all owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_sandboxes import (
    AdminSandboxArchiveRequest,
    AdminSandboxDetail,
    AdminSandboxList,
    AdminSandboxStopRequest,
    AdminSandboxTrajectoryPage,
    SandboxCursor,
    SandboxStateFilter,
    SandboxTrajectoryCursor,
    sandbox_cursor_scope,
    sandbox_trajectory_cursor_scope,
)
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    require_store,
    verify_admin_access,
)
from cloud_agent.broker_routing import (
    BrokerUrlRefused,
    broker_configuration_error,
    broker_for_host,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_archive import S3SandboxArchiveStore
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository, SandboxRepository
    from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries


logger = get_logger(__name__)


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
        rows = await broker.list_sandboxes(thread_id=sandbox.origin_thread_id, timeout=5)
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


async def _confirm_force_delete_stopped(
    detail: AdminSandboxDetail, storage: _AdminSandboxesStorage
) -> None:
    """Require fresh host proof that the exact retained container is stopped and idle."""
    sandbox = detail.sandbox
    host = await storage.host_pool.get_host(host=sandbox.host)
    try:
        broker = None if host is None else broker_for_host(host)
    except BrokerUrlRefused:
        broker = None
    if broker is None:
        logger.warning(
            "Force deletion has no configured owning broker",
            extra={
                "event": "agent_sandbox_delete_verification_failed",
                "sandbox_id": sandbox.id,
                "host": sandbox.host,
                "broker_status": 503,
                "failure_type": "configuration",
            },
        )
        raise HTTPException(
            status_code=503,
            detail=broker_configuration_error(host)
            or "The owning host's workspace broker is unavailable. Check its configuration and retry deletion.",
        )
    try:
        row = await broker.shared_chat_sandbox_status(sandbox.origin_thread_id, timeout=5)
    except WorkspaceBrokerError as exc:
        if exc.status_code in {404, 410}:
            raise HTTPException(
                status_code=409,
                detail="The host's container changed or is missing. Refresh details before force deleting it.",
            ) from None
        if exc.status_code in {401, 403}:
            message = (
                "The owning host's workspace broker rejected the control plane's credential. "
                "Match Workspace broker credential in Admin Settings with the host's broker token."
            )
        elif isinstance(exc.__cause__, httpx.TimeoutException):
            message = (
                "The owning host's workspace broker timed out while checking the sandbox. "
                "Check the broker and Docker responsiveness, then refresh details and retry deletion."
            )
        elif isinstance(exc.__cause__, httpx.HTTPError):
            message = (
                "The control plane cannot connect to the owning host's workspace broker. "
                "Check its advertised URL and reverse SSH tunnel, then retry deletion. "
                "If the host is gone, use Remove stale entry once its heartbeat is stale."
            )
        else:
            message = (
                "The owning host's workspace broker could not report the sandbox's stopped and idle "
                "status. Check broker readiness and host logs, then refresh details and retry deletion."
            )
        logger.warning(
            "Force deletion could not verify the owning sandbox",
            extra={
                "event": "agent_sandbox_delete_verification_failed",
                "sandbox_id": sandbox.id,
                "host": sandbox.host,
                "broker_status": exc.status_code,
                "failure_type": type(exc.__cause__).__name__
                if exc.__cause__
                else type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail=message,
        ) from None
    if (
        row.get("thread_id") != sandbox.origin_thread_id
        or row.get("origin_thread_id") != sandbox.origin_thread_id
        or row.get("sandbox_id") != sandbox.id
        or row.get("container_id") != sandbox.container_id
        or row.get("generation") != sandbox.generation
    ):
        raise HTTPException(
            status_code=409,
            detail="The host's container changed. Refresh details before force deleting it.",
        )
    if row.get("running") is True:
        raise HTTPException(
            status_code=409,
            detail="The host still reports this sandbox as running. Stop it before deleting it.",
        )
    if row.get("running") is not False or row.get("docker_status") != "stopped":
        raise HTTPException(
            status_code=409,
            detail="The host cannot prove that this container is cleanly stopped.",
        )
    terminal_count = row.get("terminal_count")
    operation_count = row.get("operation_count")
    counts_valid = (
        not isinstance(terminal_count, bool)
        and isinstance(terminal_count, int)
        and not isinstance(operation_count, bool)
        and isinstance(operation_count, int)
    )
    if (
        not counts_valid
        or terminal_count != 0
        or operation_count != 0
        or row.get("busy") is not False
        or row.get("group_busy") is not False
        or row.get("snapshotting") is not False
    ):
        raise HTTPException(
            status_code=409,
            detail="The host reports active or unknown sandbox work. Wait until it is idle.",
        )


def get_admin_sandbox_queries(
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxQueries:
    """Expose only metadata inventory reads to the admin route."""
    return require_store(store).admin_sandboxes


def _no_store(response: Response) -> None:
    """Keep this live cross-user inventory out of browser and proxy caches."""
    response.headers["Cache-Control"] = "no-store"


def admin_sandbox_deletion_available() -> bool:
    """Check configured archive availability without contacting S3."""
    return S3SandboxArchiveStore.from_settings().available


router = APIRouter(
    prefix="/admin/agent/sandboxes",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


@router.get("", response_model=AdminSandboxList)
async def list_admin_sandboxes(
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(100, ge=1, le=200),
    state: SandboxStateFilter = Query("running"),
    include_archived: bool = Query(False),
    queries: AdminSandboxQueries = Depends(get_admin_sandbox_queries),
) -> AdminSandboxList:
    """List lifecycle metadata, defaulting to retained running containers."""
    try:
        parsed = decode_cursor(cursor, SandboxCursor, sandbox_cursor_scope(state, include_archived))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if include_archived:
        return await queries.list_sandboxes(
            cursor=parsed, limit=limit, state=state, include_archived=True
        )
    if state == "running":
        return await queries.list_running(cursor=parsed, limit=limit)
    return await queries.list_sandboxes(cursor=parsed, limit=limit, state=state)


@router.get("/{sandbox_id}/trajectory", response_model=AdminSandboxTrajectoryPage)
async def get_admin_sandbox_trajectory(
    sandbox_id: str,
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(100, ge=1, le=200),
    queries: AdminSandboxQueries = Depends(get_admin_sandbox_queries),
) -> AdminSandboxTrajectoryPage:
    """Read one sandbox's newest-first lifecycle without private session content."""
    try:
        parsed = decode_cursor(
            cursor,
            SandboxTrajectoryCursor,
            sandbox_trajectory_cursor_scope(sandbox_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    trajectory = await queries.trajectory(sandbox_id, cursor=parsed, limit=limit)
    if trajectory is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    return trajectory


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


async def _set_admin_archived(
    sandbox_id: str,
    payload: AdminSandboxArchiveRequest,
    request: Request,
    admin: str,
    store: _AdminSandboxesStorage | None,
    *,
    archived: bool,
    allow_stale_host: bool = False,
) -> AdminSandboxDetail:
    """Keep reviewed removal and restore on the same atomic audit boundary."""
    storage = require_store(store)
    result = await storage.sandbox.set_admin_archived(
        sandbox_id=sandbox_id,
        generation=payload.generation,
        container_id=payload.container_id,
        last_activity_at=payload.last_activity_at,
        archived=archived,
        actor_id=admin,
        admin_ip=get_client_ip(request),
        allow_stale_host=allow_stale_host,
    )
    errors = {
        "not_found": (404, "Sandbox not found."),
        "stale": (409, "Sandbox activity or container changed. Refresh before changing it."),
        "not_stopped": (409, "Only a stopped sandbox can be removed from inventory."),
        "host_online": (
            409,
            "The owning host is still reporting. Refresh details or stop the sandbox before removing it.",
        ),
        "busy": (409, "Sandbox has an active turn, operation, or snapshot. Wait until it is idle."),
    }
    if result != "accepted":
        status, message = errors[result]
        raise HTTPException(status_code=status, detail=message)
    detail = await storage.admin_sandboxes.detail(sandbox_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    return detail


@router.post("/{sandbox_id}/archive", response_model=AdminSandboxDetail)
async def archive_admin_sandbox(
    sandbox_id: str,
    payload: AdminSandboxArchiveRequest,
    request: Request,
    stale: bool = Query(
        False,
        description="Remove only the inventory entry when its host has no fresh heartbeat, retaining native state.",
    ),
    admin: str = Depends(verify_admin_access),
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxDetail:
    """Remove a stopped or explicitly reviewed stale-host inventory entry."""
    return await _set_admin_archived(
        sandbox_id, payload, request, admin, store, archived=True, allow_stale_host=stale
    )


@router.post("/{sandbox_id}/restore", response_model=AdminSandboxDetail)
async def restore_admin_sandbox(
    sandbox_id: str,
    payload: AdminSandboxArchiveRequest,
    request: Request,
    admin: str = Depends(verify_admin_access),
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxDetail:
    """Return a removed sandbox to inventory without changing its lifecycle."""
    return await _set_admin_archived(sandbox_id, payload, request, admin, store, archived=False)


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


@router.post("/{sandbox_id}/delete", response_model=AdminSandboxDetail, status_code=202)
async def delete_admin_sandbox(
    sandbox_id: str,
    payload: AdminSandboxStopRequest,
    request: Request,
    force: bool = Query(False),
    admin: str = Depends(verify_admin_access),
    archive_available: bool = Depends(admin_sandbox_deletion_available),
    store: _AdminSandboxesStorage | None = Depends(get_storage),
) -> AdminSandboxDetail:
    """Queue verified deletion, with fail-closed recovery for stale running state."""
    if not archive_available:
        raise HTTPException(
            status_code=503,
            detail="Configure a sandbox archive S3 bucket in Admin Settings before deleting sandboxes.",
        )
    storage = require_store(store)
    broker_confirmed_stopped = False
    if force:
        current = await storage.admin_sandboxes.detail(sandbox_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Sandbox not found.")
        reviewed = AdminSandboxDetail.model_validate(current)
        sandbox = reviewed.sandbox
        if (
            sandbox.generation != payload.generation
            or sandbox.container_id != payload.container_id
            or sandbox.last_activity_at != payload.last_activity_at
        ):
            raise HTTPException(
                status_code=409,
                detail="Sandbox activity or container changed. Refresh before deleting it.",
            )
        if sandbox.state == "running":
            await _confirm_force_delete_stopped(reviewed, storage)
            broker_confirmed_stopped = True
        elif sandbox.state != "stopped":
            raise HTTPException(
                status_code=409,
                detail="Force deletion can only recover a sandbox still recorded as running.",
            )
    result = await storage.sandbox.request_admin_delete(
        sandbox_id=sandbox_id,
        generation=payload.generation,
        container_id=payload.container_id,
        last_activity_at=payload.last_activity_at,
        actor_id=admin,
        admin_ip=get_client_ip(request),
        broker_confirmed_stopped=broker_confirmed_stopped,
    )
    errors = {
        "not_found": (404, "Sandbox not found."),
        "stale": (409, "Sandbox activity or container changed. Refresh before deleting it."),
        "not_stopped": (409, "Stop the sandbox before archiving it to S3 and deleting it."),
        "busy": (409, "Sandbox has an active turn, operation, or snapshot. Wait until it is idle."),
    }
    if result != "accepted":
        status, message = errors[result]
        raise HTTPException(status_code=status, detail=message)
    detail = await storage.admin_sandboxes.detail(sandbox_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    return detail
