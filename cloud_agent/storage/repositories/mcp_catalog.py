"""Durable public directory cache; no credentials or user-owned configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


class PostgresMcpCatalogRepository:
    """Store one complete snapshot so readers never see half a sync."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load_mcp_catalog(self) -> dict[str, Any] | None:
        """Read the last complete snapshot and its timestamp."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT entries, synced_at FROM agent_mcp_catalog_cache WHERE singleton = TRUE"
            )
        if row is None:
            return None
        return {"entries": load_json(row["entries"]), "synced_at": row["synced_at"]}

    async def save_mcp_catalog(self, entries: list[dict[str, Any]], synced_at: datetime) -> None:
        """Atomically replace a snapshot only if this sync is newer."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_mcp_catalog_cache (singleton, entries, synced_at)
                   VALUES (TRUE, $1::jsonb, $2)
                   ON CONFLICT (singleton) DO UPDATE
                   SET entries = EXCLUDED.entries, synced_at = EXCLUDED.synced_at
                   WHERE agent_mcp_catalog_cache.synced_at < EXCLUDED.synced_at""",
                json.dumps(entries),
                synced_at,
            )
