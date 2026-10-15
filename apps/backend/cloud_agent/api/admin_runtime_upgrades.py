"""Admin endpoints for updating an agent CLI with one button.

``GET`` shows each agent's version for new chats beside npm's latest release,
with the progress of any update in flight; ``POST`` starts one, to the latest
release or any other the version list offers. ``GET …/{runtime}/versions``
lists those. Rolling back is the existing ``PUT /admin/agent/runtime-versions``,
which also replaces the update. See :mod:`cloud_agent.runtime_upgrades` for how an update completes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cloud_agent.api.deps import get_client_ip, get_storage, log_admin_action, verify_admin_access
from cloud_agent.runtime_upgrades import (
    RegistryClient,
    default_version,
    is_newer,
    update_runtimes,
    upgrade_state,
)
from cloud_agent.schemas import (
    RequestRuntimeUpgradeRequest,
    RuntimeUpgradeItem,
    RuntimeUpgradeProgress,
    RuntimeUpgradesResponse,
    RuntimeVersionsResponse,
)
from cloud_agent_shared.runtime_catalog import downloadable_runtimes

if TYPE_CHECKING:
    from cloud_agent.ports import AuditRepository, HostPoolRepository


class _UpgradeStorage(Protocol):
    audit: AuditRepository
    host_pool: HostPoolRepository


_REGISTRY = RegistryClient()


def get_registry() -> RegistryClient:
    """The process's registry client; overridden in tests."""
    return _REGISTRY


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", dependencies=[Depends(_no_store), Depends(verify_admin_access)])


def _require_store(store: _UpgradeStorage | None) -> _UpgradeStorage:
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Agent jobs are not configured for this deployment.",
        )
    return store


async def _snapshot(store: _UpgradeStorage, registry: RegistryClient) -> RuntimeUpgradesResponse:
    upgrades, selection, hosts = await store.host_pool.runtime_upgrade_snapshot()
    runtimes = update_runtimes()
    catalog = downloadable_runtimes()
    latest = await asyncio.gather(
        *(registry.latest(catalog[runtime].npm_package) for runtime in runtimes)
    )
    items: list[RuntimeUpgradeItem] = []
    for runtime, (latest_version, latest_error) in zip(runtimes, latest, strict=True):
        default = default_version(runtime, hosts)
        current = selection.get(runtime) or default
        state = upgrade_state(runtime, upgrades.get(runtime), hosts)
        # Not offered again while that same version is on its way.
        on_its_way = (
            state is not None and state.state != "updated" and state.version == latest_version
        )
        items.append(
            RuntimeUpgradeItem(
                runtime=runtime,
                current_version=current,
                default_version=default,
                latest_version=latest_version,
                latest_error=latest_error,
                update_available=is_newer(latest_version, current) and not on_its_way,
                upgrade=None
                if state is None
                else RuntimeUpgradeProgress(
                    version=state.version,
                    state=state.state,  # type: ignore[arg-type]
                    progress=state.progress,
                    error=state.error,
                    previous_version=state.previous_version,
                    requested_at=state.requested_at,
                    activated_at=state.activated_at,
                    kind=state.kind,  # type: ignore[arg-type]
                ),
            )
        )
    return RuntimeUpgradesResponse(runtimes=items)


@router.get("/agent/runtime-upgrades", response_model=RuntimeUpgradesResponse)
async def list_runtime_upgrades(
    store: _UpgradeStorage | None = Depends(get_storage),
    registry: RegistryClient = Depends(get_registry),
) -> RuntimeUpgradesResponse:
    """Each agent's version for new chats, npm's latest, and any update's progress."""
    return await _snapshot(_require_store(store), registry)


@router.get("/agent/runtime-upgrades/{runtime}/versions", response_model=RuntimeVersionsResponse)
async def list_runtime_versions(
    runtime: str,
    registry: RegistryClient = Depends(get_registry),
) -> RuntimeVersionsResponse:
    """Every stable npm release of one agent, newest first, for the version menu.

    Its own request, not part of the page's poll: one package's document is
    up to about 15 MB from npm, read once an hour, and the menu needs it once.
    """
    definition = downloadable_runtimes().get(runtime)
    if definition is None:
        raise HTTPException(status_code=422, detail=f"{runtime} cannot be updated from this page.")
    found = await registry.versions(definition.npm_package)
    return RuntimeVersionsResponse(
        runtime=runtime,
        versions=found.versions,
        deprecated=found.deprecated,
        error=found.error,
    )


@router.post("/agent/runtime-upgrades", response_model=RuntimeUpgradesResponse)
async def request_runtime_upgrade(
    request: Request,
    payload: RequestRuntimeUpgradeRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: _UpgradeStorage | None = Depends(get_storage),
    registry: RegistryClient = Depends(get_registry),
) -> RuntimeUpgradesResponse:
    """Update one agent: every host downloads and checks the version, then new chats use it.

    Any published version, older ones included. Asking again for the same
    version retries it. Existing chats keep the version they started on.
    """
    job_store = _require_store(store)
    definition = downloadable_runtimes().get(payload.runtime)
    if definition is None:
        raise HTTPException(
            status_code=422, detail=f"{payload.runtime} cannot be updated from this page."
        )
    published = await registry.has_version(definition.npm_package, payload.version)
    if published is False:
        raise HTTPException(
            status_code=422,
            detail=f"npm has no {definition.npm_package} {payload.version}.",
        )
    if published is None:
        raise HTTPException(status_code=503, detail="Could not reach the npm registry.")
    target = await job_store.host_pool.request_runtime_upgrade(
        runtime=payload.runtime, version=payload.version, requested_by=_admin_id
    )
    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runtime_version.upgrade",
        None,
        {
            "runtime": payload.runtime,
            "version": payload.version,
            "previous_version": target.get("previous_version"),
        },
        actor_id=_admin_id,
    )
    return await _snapshot(job_store, registry)
