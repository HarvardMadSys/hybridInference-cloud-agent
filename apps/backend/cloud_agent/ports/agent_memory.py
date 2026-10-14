"""Owner-scoped persistence for durable memories."""

from __future__ import annotations

from typing import Any, Protocol


class AgentMemoryRepository(Protocol):
    """Store one row per memory, scoped to its owner and optional repository."""

    async def list_memories(self, user_id: str, repo: str | None) -> dict[str, Any]:
        """Read one scope's active and archived memories and its enabled setting."""
        ...

    async def add_memory(self, user_id: str, repo: str | None, content: str) -> dict[str, Any]:
        """Save a memory the owner wrote; it takes effect immediately."""
        ...

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        repo: str | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """Rewrite or move an active memory at the version the owner examined."""
        ...

    async def delete_memory(self, user_id: str, memory_id: str, *, expected_version: int) -> None:
        """Delete one memory permanently at the version the owner examined."""
        ...

    async def restore_memory(
        self, user_id: str, memory_id: str, *, expected_version: int
    ) -> dict[str, Any]:
        """Make an archived memory active again."""
        ...

    async def clear_memories(self, user_id: str, repo: str | None) -> None:
        """Delete every memory in one scope, archived ones included."""
        ...

    async def purge_memories(self) -> None:
        """Forget archives older than the retention period."""
        ...
