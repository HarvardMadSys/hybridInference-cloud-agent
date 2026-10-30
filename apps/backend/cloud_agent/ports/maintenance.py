"""Port for the one maintenance state every control-plane process reads."""

from __future__ import annotations

from typing import Any, Protocol


class MaintenanceState(Protocol):
    """Begin, read and end maintenance; readable while the database is read-only."""

    async def current(self) -> dict[str, Any] | None:
        """The maintenance row, or ``None`` when there never was one."""
        ...

    async def begin(self, *, workflow_id: str, kind: str, detail: str) -> bool:
        """Enter maintenance for ``workflow_id``; ``False`` while another workflow holds it."""
        ...

    async def finish(self, *, workflow_id: str, state: str, detail: str) -> None:
        """Leave maintenance if ``workflow_id`` holds it."""
        ...
