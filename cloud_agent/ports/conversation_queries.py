"""User-scoped reads over conversations, jobs and projects.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class ConversationQueries(Protocol):
    """User-scoped reads over conversations, jobs and projects."""

    async def get_thread_for_job(
        *,
        job_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Return the conversation containing an owned job."""
        ...

    async def get_job(
        job_id: str,
    ) -> dict[str, Any] | None:
        """Fetch one job by id."""
        ...

    async def list_jobs(
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """List a user's jobs with pinned threads first, then newest first."""
        ...

    async def list_conversations(
        *,
        user_id: str,
        q: str = "",
        repo: str | None = None,
        include_archived: bool = False,
        pinned: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Page complete owned conversations, searching their canonical title."""
        ...

    async def list_projects(
        *,
        user_id: str,
        archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Summarize a user's repos, most recently active first."""
        ...

    async def list_sessions(
        *,
        user_id: str,
        archived: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        repo: str | None = None,
        state: str | None = None,
        include_summary: bool = True,
    ) -> dict[str, Any]:
        """Page complete conversations independently of their number of turns."""
        ...
