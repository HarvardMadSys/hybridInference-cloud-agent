"""Owner-scoped context reads behind the Context view and Context insights."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.context_insights import ContextInsightsCursor, ContextSort


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
    ) -> dict[str, Any]:
        """One ranked page of the owner's conversations with runs in the range."""
        ...
