"""Sandbox Manager access to the retained per-chat workspace.

Resolving the owning sandbox manager, restoring shared aliases, and mapping private
sandbox manager failures onto the bounded owner-facing errors the workspace, job, turn
and worker surfaces all return. The terminal-id pattern travels with it: the
path parameters that name a terminal are validated from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException

from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import (
    hydrate_shared_sandbox_manager,
    sandbox_manager_for_job,
)
from cloud_agent.workspace_errors import (
    _sandbox_manager_http_error,
)

if TYPE_CHECKING:
    from typing import Any

    from cloud_agent.ports import HostPoolRepository
    from cloud_agent.sandbox_manager_client import SandboxManagerClient


class _WorkspaceAccessStorage(Protocol):
    """What resolving a job's sandbox manager needs from the storage assembly.

    Narrowed to the one capability the shared helpers below actually reach for.
    A helper shared by several routers must not name the aggregator's
    twelve-capability protocol, which is what the design requires and what the
    module boundary makes unavoidable.
    """

    host_pool: HostPoolRepository


_TERMINAL_ID_PATTERN = r"^term_[A-Za-z0-9_-]{1,80}$"


def _workspace_id(job: dict[str, Any]) -> str:
    """Address the chat member inside its retained shared container."""
    thread_id = job.get("thread_id")
    if not thread_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This chat has no retained workspace.",
                }
            },
        )
    return str(thread_id)


async def _sandbox_manager_for_job(
    store: _WorkspaceAccessStorage,
    job: dict[str, Any],
) -> SandboxManagerClient | None:
    """Resolve the owning sandbox manager and restore shared aliases before use."""
    sandbox_manager = await sandbox_manager_for_job(store.host_pool, job["id"])
    try:
        return await hydrate_shared_sandbox_manager(job, sandbox_manager)
    except SandboxManagerError as exc:
        raise _sandbox_manager_http_error(exc) from exc


def _workspace_not_ready() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "workspace_not_ready",
                "message": "The workspace is temporarily locked while the agent prepares or saves it.",
            }
        },
    )
