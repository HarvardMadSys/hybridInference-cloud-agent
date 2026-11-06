"""Existing hosts and policy gain empty admin settings, and lose them cleanly on downgrade."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.domain.host_pool import hash_secret

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0027_compute_resources"
_THIS = "0028_host_settings"


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
    )
    return {row["column_name"] for row in rows}


async def test_existing_rows_start_with_no_admin_values_and_no_report(store):
    """Every host keeps reading its .env until an admin sets something."""
    await store.host_pool.register_host(host="runner-a", credential_hash=hash_secret("a"))
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "settings_desired" not in await _columns(conn, "agent_runner_hosts")
        assert "host_settings" not in await _columns(conn, "agent_runner_policy")
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        row = await conn.fetchrow(
            "SELECT settings_desired::text AS desired, settings_applied, settings_local "
            "FROM agent_runner_hosts WHERE host = 'runner-a'"
        )
        assert (row["desired"], row["settings_applied"], row["settings_local"]) == (
            "{}",
            None,
            None,
        )
        assert await conn.fetchval("SELECT host_settings::text FROM agent_runner_policy") == "{}"
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        host_columns = await _columns(conn, "agent_runner_hosts")
        assert not {"settings_desired", "settings_applied", "settings_local"} & host_columns
        assert "host_settings" not in await _columns(conn, "agent_runner_policy")
        host = await conn.fetchval("SELECT host FROM agent_runner_hosts WHERE host = 'runner-a'")
        assert host == "runner-a"
    finally:
        await conn.close()
    apply_migrations(config)
