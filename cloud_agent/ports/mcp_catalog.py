"""Persistence for the public MCP directory, separate from user connections."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class McpCatalogRepository(Protocol):
    """Keep the last complete directory across restarts and failed syncs."""

    async def load_mcp_catalog(self) -> dict[str, Any] | None:
        """Read the last complete snapshot and its timestamp."""
        ...

    async def save_mcp_catalog(self, entries: list[dict[str, Any]], synced_at: datetime) -> None:
        """Atomically replace a snapshot only if this sync is newer."""
        ...
