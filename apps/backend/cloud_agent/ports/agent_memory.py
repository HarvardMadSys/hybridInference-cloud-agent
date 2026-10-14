"""Owner-scoped persistence for durable memories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.agent_memory import MemoryTask


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

    async def set_memory_enabled(
        self, user_id: str, repo: str | None, enabled: bool
    ) -> dict[str, Any]:
        """Turn one scope's memory on or off; its memories are kept either way."""
        ...

    async def memory_projects(self, user_id: str) -> list[str]:
        """Repositories the owner worked in or keeps memories or a switch for."""
        ...

    async def clear_memories(self, user_id: str, repo: str | None) -> None:
        """Delete every memory in one scope, archived ones included."""
        ...

    async def task_memories(self, user_id: str, repo: str | None) -> dict[str, Any]:
        """Return the active memories a task in ``repo`` follows, newest first per scope."""
        ...

    async def mark_memories_used(self, user_id: str, memory_ids: list[str]) -> None:
        """Record that these memories were given to a task."""
        ...

    async def save_agent_memory(
        self, task: MemoryTask, content: str, *, all_projects: bool
    ) -> dict[str, Any]:
        """Save a memory an agent wrote during ``task``, while its attempt runs."""
        ...

    async def replace_agent_memory(
        self, task: MemoryTask, memory_id: str, content: str
    ) -> dict[str, Any]:
        """Replace a memory the task can see with new text, archiving the old one."""
        ...

    async def archive_agent_memory(self, task: MemoryTask, memory_id: str) -> dict[str, Any]:
        """Archive a memory the task can see; the owner can restore it."""
        ...

    async def purge_memories(self) -> None:
        """Forget archives older than the retention period."""
        ...
