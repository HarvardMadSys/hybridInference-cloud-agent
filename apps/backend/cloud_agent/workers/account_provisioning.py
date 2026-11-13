"""The pass that keeps inference provisioning in step with Agent access.

Runs only in the process that owns background work.
See :mod:`cloud_agent.application.account_provisioning` for what one pass does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.application.account_provisioning import (
    PROVISIONING_INTERVAL_S,
    run_provisioning_pass,
)
from cloud_agent.workers.periodic import run_every, run_pass

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage


async def run_account_provisioning(
    storage: Storage,
    mailer: Any,
    *,
    interval_seconds: float = PROVISIONING_INTERVAL_S,
) -> None:
    """Repeat the provisioning pass until cancelled."""

    async def one_pass() -> None:
        await run_pass(
            lambda: run_provisioning_pass(storage, mailer=mailer),
            deadline_seconds=max(interval_seconds, 10.0),
            description="Account provisioning",
            event="agent_account_provisioning",
        )

    await run_every(interval_seconds, one_pass)


__all__ = ["run_account_provisioning"]
