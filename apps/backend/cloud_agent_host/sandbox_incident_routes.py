"""The sandbox manager's side of sandbox incidents: reading journals, and confirming rows.

``sandbox_incidents_for`` builds the observer with a reader that runs the
journal drain in a running group's container, as root in the ``agent`` group:
the journals are group-readable, and root needs no capability for that.
``install_incident_routes`` adds the one route the runner calls, after a
heartbeat answer confirmed what its beat carried: the sandbox manager forgets
those rows (``cloud_agent_host.incident_relay``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Body, Depends

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_incidents import DRAIN_SCRIPT, SandboxIncidents

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from cloud_agent_host.chat_sandbox import ChatSandboxManager

#: One drain is a short read of a few small files.
DRAIN_TIMEOUT_S = 15
ROOT_UID = 0
MAX_CONFIRMED = 4096


def sandbox_incidents_for(
    supervisor: Callable[[], Any], manager: ChatSandboxManager | None
) -> SandboxIncidents:
    """The observer, reading journals through ``manager`` in groups ``supervisor`` runs."""

    def read_journals(sandbox_id: str, request: bytes) -> bytes | None:
        owner = supervisor()
        found = None if manager is None or owner is None else owner.running_group(sandbox_id)
        if found is None or manager is None:
            return None
        spec, _container_id = found
        try:
            result = manager.exec_capture(
                spec,
                ["python3", "-I", "-c", DRAIN_SCRIPT],
                workdir="/",
                timeout_s=DRAIN_TIMEOUT_S,
                input_bytes=request,
                user_uid=ROOT_UID,
                user_gid=manager.backend.gid,
            )
        except SandboxError:
            return None
        return result.stdout if result.returncode == 0 else None

    return SandboxIncidents(read_journals=read_journals)


def install_incident_routes(
    app: FastAPI, incidents: SandboxIncidents, authenticate: Callable[..., None]
) -> None:
    """Add ``POST /sandbox-incidents/confirm``: forget rows the control plane stored."""

    @app.post("/sandbox-incidents/confirm")
    def confirm_incidents(
        body: dict[str, Any] = Body(...),
        _: None = Depends(authenticate),
    ) -> dict[str, int]:
        confirmed = body.get("confirmed")
        if not isinstance(confirmed, dict):
            return {"confirmed": 0}
        rows = {
            str(sandbox_id): [str(item) for item in ids[:MAX_CONFIRMED]]
            for sandbox_id, ids in list(confirmed.items())[:MAX_CONFIRMED]
            if isinstance(ids, list)
        }
        incidents.confirm(rows)
        return {"confirmed": sum(len(ids) for ids in rows.values())}


__all__ = ["install_incident_routes", "sandbox_incidents_for"]
