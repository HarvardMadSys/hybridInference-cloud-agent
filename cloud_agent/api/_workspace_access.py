"""Broker access to the retained per-chat workspace.

Resolving the owning broker, restoring shared aliases, and mapping private
broker failures onto the bounded owner-facing errors the workspace, job, turn
and worker surfaces all return. The terminal-id pattern travels with it: the
path parameters that name a terminal are validated from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException

from cloud_agent.broker_routing import broker_for_job, hydrate_shared_broker
from cloud_agent.workspace_broker_client import WorkspaceBrokerError
from cloud_agent.workspace_errors import (
    _workspace_broker_http_error,
)

if TYPE_CHECKING:
    from typing import Any

    from cloud_agent.ports import HostPoolRepository
    from cloud_agent.workspace_broker_client import WorkspaceBrokerClient


class _WorkspaceAccessStorage(Protocol):
    """What resolving a job's broker needs from the storage assembly.

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


async def _workspace_broker_for_job(
    store: _WorkspaceAccessStorage,
    job: dict[str, Any],
) -> WorkspaceBrokerClient | None:
    """Resolve the owning broker and restore shared aliases before use."""
    broker = await broker_for_job(store.host_pool, job["id"])
    try:
        return await hydrate_shared_broker(job, broker)
    except WorkspaceBrokerError as exc:
        raise _workspace_broker_http_error(exc) from exc


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
