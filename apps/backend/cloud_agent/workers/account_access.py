"""The local-mode pass that keeps ended Agent access ended.

Runs only in the process that owns background work, and only in local mode.
See :mod:`cloud_agent.application.owner_termination` for what one pass does
and the stopping bound it gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.application.owner_termination import (
    ENFORCEMENT_INTERVAL_S,
    enforce_ended_access,
)
from cloud_agent.workers.periodic import run_every, run_pass

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage


async def run_account_access_enforcement(
    storage: Storage, grants: Any, *, interval_seconds: float = ENFORCEMENT_INTERVAL_S
) -> None:
    """Repeat the enforcement pass until cancelled."""

    async def one_pass() -> None:
        await run_pass(
            lambda: enforce_ended_access(storage, grants),
            deadline_seconds=max(interval_seconds, 10.0),
            description="Local account access enforcement",
            event="agent_account_access_enforcement",
        )

    await run_every(interval_seconds, one_pass)


__all__ = ["run_account_access_enforcement"]
