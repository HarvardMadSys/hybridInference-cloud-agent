"""The semantic configuration catalog and its live state, for administrators.

``GET /admin/agent/management/catalog`` describes every administrator-facing
configuration item: how it is managed, where each of its meanings comes from,
and when a change takes effect. It never returns a value. Live state is
served beside it by the status endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from cloud_agent.api.deps import get_storage, require_store, verify_admin_access
from cloud_agent.management_catalog import catalog_response


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/management",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


@router.get("/catalog")
async def read_catalog() -> dict[str, Any]:
    """Every configuration item and operation, without values."""
    return catalog_response()


def _instance_view(instance: dict[str, Any], saved_revision: int) -> dict[str, Any]:
    details = instance.get("details") or {}
    return {
        "instance_id": instance["instance_id"],
        "role": instance["role"],
        "machine": instance["machine"],
        "build_sha": instance["build_sha"],
        "capabilities": instance["capabilities"],
        "adopted_settings_revision": instance["adopted_settings_revision"],
        "adopted": bool(instance["current"])
        and instance["adopted_settings_revision"] >= saved_revision,
        "restart_required": instance["restart_required"],
        "current": bool(instance["current"]),
        "stopped": instance.get("stopped_at") is not None,
        "last_seen_at": instance["last_seen_at"],
        "root_key_source": details.get("root_key_source"),
        "root_key_id": details.get("root_key_id"),
    }


def _host_view(host: dict[str, Any]) -> dict[str, Any]:
    desired = host.get("settings_desired") or {}
    applied = host.get("settings_applied")
    supports = "host-settings-v1" in (host.get("capabilities") or [])
    return {
        "host": host.get("host"),
        "status": host.get("status"),
        "reports_settings": supports and applied is not None,
        "settings_pending": sorted(
            key for key, value in desired.items() if applied is None or applied.get(key) != value
        ),
        "config_reports": host.get("config_reports") or {},
    }


@router.get("/status")
async def read_status(store: Any = Depends(get_storage)) -> dict[str, Any]:
    """Saved versus adopted state, from the reports of every process and host.

    ``adopted`` counts only current reports at or past the saved revision;
    a stale, stopped or unreporting participant is never counted, and the
    database sessions no reporting process opened are listed beside them.
    """
    storage = require_store(store)
    saved_revision, _encoded = await storage.settings.read_settings()
    instances = await storage.instances.list_instances()
    sessions = await storage.instances.unidentified_sessions()
    views = [_instance_view(instance, saved_revision) for instance in instances]
    current = [view for view in views if view["current"]]
    restart: dict[str, list[str]] = {}
    for view in current:
        for key in view["restart_required"]:
            restart.setdefault(key, []).append(f"{view['role']}@{view['machine']}")
    hosts = await storage.host_pool.list_runner_hosts()
    return {
        "settings": {
            "saved_revision": saved_revision,
            "reporting": len(current),
            "adopted": sum(1 for view in current if view["adopted"]),
            "restart_required": restart,
        },
        "instances": views,
        "unidentified_sessions": [
            {
                "application_name": session.get("application_name") or "",
                "user": session.get("usename") or "",
                "client_addr": session.get("client_addr") or "",
                "backend_start": session.get("backend_start"),
            }
            for session in sessions
        ],
        "hosts": [_host_view(host) for host in hosts],
    }
