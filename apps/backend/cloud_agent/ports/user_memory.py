"""Owner-scoped persistence for automatically derived user memory."""

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

    async def refresh_active_users(self, limit: int = 100) -> int:
        """Visit a bounded, fair batch of eligible owners and return its size."""
        ...
