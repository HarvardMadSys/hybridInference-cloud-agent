"""Owner-scoped reads behind the home page's work insights."""

from __future__ import annotations

from typing import Protocol


class WorkInsightsRepository(Protocol):
    """Read the requests the home page's insights are derived from."""

    async def recent_requests(self, user_id: str, limit: int = 50) -> list[str]:
        """The owner's latest eligible requests, newest first."""
        ...
