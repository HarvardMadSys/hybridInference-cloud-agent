"""Port for the persisted, deployment-wide administrator settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class SettingsRepository(Protocol):
    """Read, initialize and atomically patch the deployment's settings."""

    async def read_settings(self, *, after_revision: int = -1) -> tuple[int, dict[str, Any]]:
        """Read a newer snapshot, or ``(-1, {})`` when none is available."""
        ...

    async def seed_settings(self, values: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Initialize or import new fields once, preserving every saved value."""
        ...

    async def update_settings(
        self, changes: Mapping[str, Any], *, expected_revision: int | None = None
    ) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Atomically merge encoded changes and return revision, current and previous values.

        With ``expected_revision``, raise ``SettingsRevisionConflict`` unless
        the stored settings are still at that revision.
        """
        ...
