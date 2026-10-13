"""Owner-scoped persistence for automatically derived user and project memory."""

from __future__ import annotations

from typing import Any, Protocol


class UserMemoryRepository(Protocol):
    """Read and control one user's recent-work portrait and memory."""

    async def refresh(self, user_id: str) -> dict[str, Any]:
        """Create or update a profile from its owner's recent user messages."""
        ...

    async def get(self, user_id: str) -> dict[str, Any] | None:
        """Read the saved public profile without processing activity."""
        ...

    async def set_enabled(self, user_id: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable generation and use of this owner's memory."""
        ...

    async def clear(self, user_id: str) -> dict[str, Any]:
        """Forget saved knowledge and exclude messages predating this reset."""
        ...

    async def update_entry(
        self, user_id: str, memory_id: str, *, content: str, expected_revision: int
    ) -> dict[str, Any]:
        """Correct an existing owned entry at the profile revision the user read."""
        ...

    async def delete_entry(
        self, user_id: str, memory_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        """Remove an existing entry durably at the profile revision the user read."""
        ...

    async def replace_memories(
        self, user_id: str, revision: int, memories: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Save source-backed edits only at the current revision."""
        ...

    async def refresh_project(self, user_id: str, repo: str) -> dict[str, Any]:
        """Create or update only this owner's selected project profile."""
        ...

    async def get_project(self, user_id: str, repo: str) -> dict[str, Any] | None:
        """Read the owner's saved project profile without processing activity."""
        ...

    async def set_project_enabled(self, user_id: str, repo: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable generation and use for only this project."""
        ...

    async def clear_project(self, user_id: str, repo: str) -> dict[str, Any]:
        """Forget one project's saved knowledge and fence its prior messages."""
        ...

    async def update_project_entry(
        self, user_id: str, repo: str, memory_id: str, *, content: str, expected_revision: int
    ) -> dict[str, Any]:
        """Correct an owned project entry at its current profile revision."""
        ...

    async def delete_project_entry(
        self, user_id: str, repo: str, memory_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        """Remove an owned project entry durably at its current revision."""
        ...

    async def replace_project_memories(
        self, user_id: str, repo: str, revision: int, memories: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Save project edits and explicit scope changes at its current revision."""
        ...

    async def list_memory_projects(self, user_id: str) -> list[str]:
        """List repositories from this owner's history and saved project profiles."""
        ...

    async def refresh_active_users(self, limit: int = 100) -> int:
        """Visit a bounded, fair batch of eligible owners and return its size."""
        ...
