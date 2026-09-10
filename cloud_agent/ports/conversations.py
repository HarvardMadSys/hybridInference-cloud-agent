"""Creating conversations and changing their metadata.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class ConversationCommands(Protocol):
    """Creating conversations and changing their metadata."""

    async def create_job(
        *,
        user_id: str,
        repo: str,
        task_prompt: str,
        runtime: str,
        model: str,
        execution_profile: str | None = None,
        reasoning_effort: str | None = None,
        base_sha: str | None = None,
        setup_script: str | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it."""
        ...

    async def create_follow_up(
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reset_reasoning_effort: bool = False,
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it."""
        ...

    async def set_sessions_archived(
        *,
        thread_ids: list[str],
        user_id: str,
        archived: bool,
    ) -> dict[str, Any]:
        """Set every owned selected thread's archive state, or change none."""
        ...

    async def delete_sessions(
        *,
        thread_ids: list[str],
        user_id: str,
    ) -> dict[str, Any]:
        """Irreversibly hide settled archived history while retaining audit rows."""
        ...

    async def set_thread_archived(
        *,
        job_id: str,
        user_id: str,
        archived: bool,
    ) -> dict[str, Any] | None:
        """Archive or restore the owned thread containing ``job_id``."""
        ...

    async def set_thread_title(
        *,
        job_id: str,
        user_id: str,
        title: str,
    ) -> dict[str, Any] | None:
        """Rename the owned conversation without changing any turn's prompt."""
        ...

    async def set_generated_thread_title(
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        title: str,
    ) -> bool:
        """Save a first-turn title while its running/successful attempt remains current."""
        ...

    async def set_thread_pinned(
        *,
        job_id: str,
        user_id: str,
        pinned: bool,
    ) -> dict[str, Any] | None:
        """Pin or unpin the owned thread containing ``job_id``."""
        ...
