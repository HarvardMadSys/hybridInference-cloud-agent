"""Port for the persisted, deployment-wide administrator settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class SettingsRepository(Protocol):
    """Read, initialize and atomically patch the deployment's settings."""

    async def read_settings(self) -> tuple[int, dict[str, Any]]:
        """Return the revision and encoded values, or revision minus one before initialization."""
        ...

    async def seed_settings(self, values: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Initialize or import new fields once, preserving every saved value."""
        ...

    async def update_settings(
        self, changes: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Atomically merge encoded changes and return revision, current and previous values."""
        ...
