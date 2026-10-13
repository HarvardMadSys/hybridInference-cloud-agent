"""Durable public directory cache; no credentials or user-owned configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.ports.mcp_catalog import McpCatalogSave, McpCatalogSync
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


class PostgresMcpCatalogRepository:
    """Store one complete snapshot so readers never see half a sync."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load_mcp_catalog(self) -> dict[str, Any] | None:
        """Read the last complete snapshot and its source fence."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT entries, synced_at, registry_url, settings_revision, generation
                   FROM agent_mcp_catalog_snapshots WHERE singleton = TRUE"""
            )
        if row is None:
            return None
        return {
            "entries": load_json(row["entries"]),
            "synced_at": row["synced_at"],
            "registry_url": row["registry_url"],
            "settings_revision": row["settings_revision"],
            "generation": int(row["generation"]),
        }

    async def begin_mcp_catalog_sync(self) -> McpCatalogSync:
        """Issue a database-ordered generation for the current Registry setting."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT revision, "values" ->> 'mcp_registry_url' AS registry_url,
                          nextval('agent_mcp_catalog_generation_seq') AS generation
                   FROM agent_settings WHERE id = TRUE"""
            )
        if row is None or not isinstance(row["registry_url"], str):
            raise RuntimeError("MCP Registry setting is not initialized")
        return McpCatalogSync(
            registry_url=row["registry_url"].rstrip("/"),
            settings_revision=int(row["revision"]),
            generation=int(row["generation"]),
        )

    async def save_mcp_catalog(
        self, entries: list[dict[str, Any]], sync: McpCatalogSync
    ) -> McpCatalogSave:
        """Publish only while the scan's authoritative settings still hold.

        The shared lock makes the settings check and cache write one ordered
        operation with an administrator's settings update. The database-issued
        generation, rather than a process clock, fences concurrent scanners.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                """SELECT revision, "values" ->> 'mcp_registry_url' AS registry_url
                   FROM agent_settings WHERE id = TRUE FOR SHARE"""
            )
            if current is None or not isinstance(current["registry_url"], str):
                raise RuntimeError("MCP Registry setting is not initialized")
            current_registry_url = current["registry_url"].rstrip("/")
            current_settings_revision = int(current["revision"])
            if (
                current_settings_revision != sync.settings_revision
                or current_registry_url != sync.registry_url
            ):
                return McpCatalogSave(
                    None,
                    registry_url=current_registry_url,
                    settings_revision=current_settings_revision,
                )
            saved_at = await conn.fetchval(
                """INSERT INTO agent_mcp_catalog_snapshots
                          (singleton, entries, synced_at, registry_url,
                           settings_revision, generation)
                   VALUES (TRUE, $1::jsonb, statement_timestamp(), $2, $3, $4)
                   ON CONFLICT (singleton) DO UPDATE
                   SET entries = EXCLUDED.entries,
                       synced_at = EXCLUDED.synced_at,
                       registry_url = EXCLUDED.registry_url,
                       settings_revision = EXCLUDED.settings_revision,
                       generation = EXCLUDED.generation
                   WHERE agent_mcp_catalog_snapshots.generation < EXCLUDED.generation
                   RETURNING synced_at""",
                json.dumps(entries),
                sync.registry_url,
                sync.settings_revision,
                sync.generation,
            )
        return McpCatalogSave(
            saved_at,
            registry_url=current_registry_url,
            settings_revision=current_settings_revision,
        )
