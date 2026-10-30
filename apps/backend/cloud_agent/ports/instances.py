"""Port for the control-plane processes' reports about themselves."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime


class InstanceReports(Protocol):
    """Write this process's report and read every process's."""

    async def report(
        self,
        *,
        instance_id: str,
        role: str,
        machine: str,
        build_sha: str,
        capabilities: Sequence[str],
        adopted_settings_revision: int,
        restart_required: Sequence[str],
        details: Mapping[str, Any],
        started_at: datetime,
    ) -> None:
        """Insert or refresh this process's row."""
        ...

    async def stop(self, instance_id: str) -> None:
        """Mark this process stopped on a clean shutdown."""
        ...

    async def list_instances(self) -> list[dict[str, Any]]:
        """Every retained report, with whether it is current."""
        ...

    async def unidentified_sessions(self) -> list[dict[str, Any]]:
        """Database sessions no reporting control-plane process opened."""
        ...
