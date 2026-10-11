"""Existing deployments gain a disabled archive policy and no false host report."""

from __future__ import annotations

import json

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.host_pool import hash_secret

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0041_sandbox_disk_limit"
_THIS = "0042_sandbox_archive_settings"


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
    )
    return {row["column_name"] for row in rows}


async def test_archive_policy_upgrade_defaults_disabled_and_downgrades_cleanly(store):
    await store.host_pool.register_host(host="runner-a", credential_hash=hash_secret("a"))
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "sandbox_archive_settings" not in await _columns(conn, "agent_runner_policy")
        assert "sandbox_archive_settings_applied_revision" not in await _columns(
            conn, "agent_runner_hosts"
        )
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        policy = await conn.fetchrow(
            """
            SELECT sandbox_archive_settings::text AS sandbox_archive_settings,
                   sandbox_archive_revision,
                   sandbox_archive_updated_at
            FROM agent_runner_policy WHERE id
            """
        )
        assert json.loads(policy["sandbox_archive_settings"])["enabled"] is False
        assert policy["sandbox_archive_revision"] == 0
        assert policy["sandbox_archive_updated_at"] is not None
        assert (
            await conn.fetchval(
                "SELECT sandbox_archive_settings_applied_revision "
                "FROM agent_runner_hosts WHERE host = 'runner-a'"
            )
            is None
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_policy SET sandbox_archive_revision = -1 WHERE id"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_policy SET sandbox_archive_settings = '[]'::jsonb WHERE id"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts "
                "SET sandbox_archive_settings_applied_revision = -1 WHERE host = 'runner-a'"
            )
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "sandbox_archive_settings" not in await _columns(conn, "agent_runner_policy")
        assert "sandbox_archive_settings_applied_revision" not in await _columns(
            conn, "agent_runner_hosts"
        )
        assert (
            await conn.fetchval("SELECT host FROM agent_runner_hosts WHERE host = 'runner-a'")
            == "runner-a"
        )
    finally:
        await conn.close()
    apply_migrations(config)
