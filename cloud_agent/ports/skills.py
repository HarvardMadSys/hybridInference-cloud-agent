"""Port for an owner's skill library, independent of execution storage."""

from __future__ import annotations

from typing import Any, Protocol


class SkillRepository(Protocol):
    """Owner-scoped reads and revision-fenced library writes."""

    async def list_skills(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return the owner's live skills, including their instruction bodies."""
        ...

    async def get_skill(self, *, user_id: str, skill_id: int) -> dict[str, Any] | None:
        """Read a live skill only when it belongs to this owner."""
        ...

    async def create_skill(self, *, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a skill, enforcing names, library size, and default limits."""
        ...

    async def update_skill(
        self, *, user_id: str, skill_id: int, revision: int, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a live skill at the revision read, or return None if absent."""
        ...

    async def delete_skill(self, *, user_id: str, skill_id: int, revision: int) -> bool:
        """Soft-delete at the expected revision, leaving turn snapshots intact."""
        ...
