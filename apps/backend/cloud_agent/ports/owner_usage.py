"""Owner-scoped reads behind the Usage settings page."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.owner_usage import (
        OwnerContainerCursor,
        OwnerContainerList,
        OwnerResourceHistory,
    )


class OwnerUsageQueries(Protocol):
    """Read one owner's containers and their past use; nothing is written."""

    async def containers(
        self, user_id: str, *, cursor: OwnerContainerCursor | None, limit: int
    ) -> OwnerContainerList:
        """A page of the owner's containers, active first, with counts over all."""
        ...

    async def resource_history(
        self, user_id: str, *, buckets: list[tuple[datetime, datetime]]
    ) -> OwnerResourceHistory:
        """Running time and measured CPU and memory for each UTC day bucket."""
        ...
