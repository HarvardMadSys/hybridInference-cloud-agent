"""Owner-scoped context reads behind the Context view and Context insights."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.context_dna import DnaBands
    from cloud_agent.context_insights import ContextInsightsCursor, ContextSort, InsightsFilters
    from cloud_agent.context_request import RequestComposition


class ContextQueries(Protocol):
    """Read one owner's context profiles, deriving stale ones from the ledger."""

    async def thread_context(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """The caller's conversation holding ``job_id``: its jobs and attempt profiles."""
        ...

    async def insights_page(
        self,
        *,
        user_id: str,
        since: datetime,
        until: datetime,
        sort: ContextSort,
        cursor: ContextInsightsCursor | None,
        limit: int,
        refresh_attempts: int,
        refresh_events: int,
        pending_cap: int,
        filters: InsightsFilters | None = None,
    ) -> dict[str, Any]:
        """One ranked page of the owner's conversations with runs in the range."""
        ...

    async def request_composition(
        self, *, job_id: str, user_id: str, event_id: int
    ) -> RequestComposition | None:
        """What one request carried, in the caller's conversation holding ``job_id``."""
        ...

    async def dna_bands(
        self, *, job_id: str, user_id: str, first: int, last: int
    ) -> DnaBands | None:
        """The items of a window of requests, in the caller's conversation."""
        ...

    async def event_content(
        self, *, job_id: str, user_id: str, event_id: int, offset: int, limit: int
    ) -> dict[str, Any] | None:
        """A window of one stored content event's text, in the caller's conversation."""
        ...
