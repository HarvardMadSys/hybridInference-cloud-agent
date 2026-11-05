"""Retain notification change markers independently of owner reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.workers.periodic import run_every, run_pass

if TYPE_CHECKING:
    from cloud_agent.ports import NotificationMaintenanceRepository


NOTIFICATION_MAINTENANCE_INTERVAL_S = 60.0
NOTIFICATION_MAINTENANCE_DEADLINE_S = 10.0
NOTIFICATION_MAINTENANCE_BATCH_SIZE = 500


async def run_notification_maintenance(
    maintenance: NotificationMaintenanceRepository,
    *,
    interval_seconds: float = NOTIFICATION_MAINTENANCE_INTERVAL_S,
    deadline_seconds: float = NOTIFICATION_MAINTENANCE_DEADLINE_S,
    batch_size: int = NOTIFICATION_MAINTENANCE_BATCH_SIZE,
) -> None:
    """Drain expired markers within each periodic pass's deadline."""
    if batch_size <= 0:
        raise ValueError("notification maintenance batch size must be positive")

    async def drain() -> None:
        while await maintenance.prune_notification_changes(limit=batch_size) >= batch_size:
            pass

    async def prune() -> None:
        await run_pass(
            drain,
            deadline_seconds=deadline_seconds,
            description="Notification marker maintenance sweep",
            event="agent_notification_maintenance",
        )

    await run_every(interval_seconds, prune)
