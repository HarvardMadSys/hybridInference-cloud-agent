"""Persistence for the public MCP directory, separate from user connections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class McpCatalogSync:
    """Database-issued authority for one resumable Registry scan."""

    registry_url: str
    settings_revision: int
    generation: int


@dataclass(frozen=True)
class McpCatalogSave:
    """Outcome plus the settings authority observed by the fenced write."""

    saved_at: datetime | None
    registry_url: str
    settings_revision: int


class McpCatalogRepository(Protocol):
    """Keep the last complete directory across restarts and failed syncs."""

    async def load_mcp_catalog(self) -> dict[str, Any] | None:
        """Read the last complete snapshot and its source fence."""
        ...

    async def begin_mcp_catalog_sync(self) -> McpCatalogSync:
        """Issue a monotonic scan generation for the authoritative settings row."""
        ...

    async def save_mcp_catalog(
        self, entries: list[dict[str, Any]], sync: McpCatalogSync
    ) -> McpCatalogSave:
        """Publish if allowed and return the source checked under the settings lock."""
        ...
