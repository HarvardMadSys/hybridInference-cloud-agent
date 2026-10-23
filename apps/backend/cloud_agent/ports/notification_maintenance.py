"""Port for retention of durable owner notification markers."""

from __future__ import annotations

from typing import Protocol


class NotificationMaintenanceRepository(Protocol):
    """Bounded retention operations owned by the background worker."""

    async def prune_notification_changes(self, *, limit: int = 500) -> int:
        """Delete at most the requested number of expired markers and return the count."""
        ...
