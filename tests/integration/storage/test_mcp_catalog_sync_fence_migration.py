"""The fenced MCP catalog cache stays safe across a rolling schema change."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_PREVIOUS = "0060_admin_sandbox_discard"


async def test_fenced_cache_migration_preserves_legacy_snapshot_both_directions(pool) -> None:
    """Old and new processes use isolated tables without losing the last snapshot."""
    config = _db_config()
    downgrade_to(config, _PREVIOUS)
    legacy_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    fenced_time = datetime(2025, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_mcp_catalog_cache (singleton, entries, synced_at)
                   VALUES (TRUE, '[{"name":"com.example/legacy"}]'::jsonb, $1)""",
                legacy_time,
            )
        apply_migrations(config)
        async with pool.acquire() as conn:
            copied = await conn.fetchrow(
                """SELECT entries, synced_at, registry_url, settings_revision, generation
                   FROM agent_mcp_catalog_snapshots WHERE singleton = TRUE"""
            )
            assert copied is not None
            assert copied["entries"] == '[{"name": "com.example/legacy"}]'
            assert copied["synced_at"] == legacy_time
            assert copied["registry_url"] is None
            assert copied["settings_revision"] is None
            assert copied["generation"] == 0
            await conn.execute(
                """UPDATE agent_mcp_catalog_snapshots
                   SET entries = '[{"name":"com.example/fenced"}]'::jsonb,
                       synced_at = $1,
                       registry_url = 'https://registry.example',
                       settings_revision = 7,
                       generation = 9
                   WHERE singleton = TRUE""",
                fenced_time,
            )
            # A process from the prior release still targets only the legacy
            # table, so its later write cannot corrupt the fenced snapshot.
            await conn.execute(
                """UPDATE agent_mcp_catalog_cache
                   SET entries = '[{"name":"com.example/rolling-writer"}]'::jsonb,
                       synced_at = NOW()
                   WHERE singleton = TRUE"""
            )
            assert (
                await conn.fetchval(
                    "SELECT entries FROM agent_mcp_catalog_snapshots WHERE singleton = TRUE"
                )
                == '[{"name": "com.example/fenced"}]'
            )
        downgrade_to(config, _PREVIOUS)
        async with pool.acquire() as conn:
            restored = await conn.fetchrow(
                """SELECT entries, synced_at FROM agent_mcp_catalog_cache
                   WHERE singleton = TRUE"""
            )
        assert restored is not None
        assert restored["entries"] == '[{"name": "com.example/fenced"}]'
        assert restored["synced_at"] == fenced_time
    finally:
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
