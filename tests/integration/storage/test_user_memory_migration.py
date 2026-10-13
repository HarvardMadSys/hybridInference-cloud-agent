"""User-memory schema upgrades preserve source data and round-trip safely."""

from __future__ import annotations

import json

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_user_memory_upgrade_defaults_constraints_and_round_trip_preserve_sources(
    store, pool
):
    job = await _create_job(store, user_id="alice", task_prompt="I prefer readable API tests.")
    config = _db_config()
    try:
        downgrade_to(config, "0046_job_claim_retry")
        # Compare the source tables in the historical schema under test;
        # later migrations may add columns that this rollback removes.
        async with pool.acquire() as conn:
            sources = {
                table: [dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("agent_jobs", "agent_threads", "agent_thread_messages")
            }
        for _ in range(2):
            apply_migrations(config, "0047_user_memory")
            async with pool.acquire() as conn:
                versions = {
                    row["version_num"]
                    for row in await conn.fetch("SELECT version_num FROM alembic_version")
                }
                assert versions == {
                    "0046_admin_sandbox_trajectory",
                    "0047_user_memory",
                }
                assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memories") == 0
                row = await conn.fetchrow(
                    "INSERT INTO agent_user_memories (user_id, portrait) "
                    "VALUES ('alice', '{\"summary\":\"Existing source remains.\"}'::jsonb) RETURNING *"
                )
                assert row["enabled"] is True
                assert json.loads(row["portrait"]) == {"summary": "Existing source remains."}
                assert json.loads(row["memories"]) == json.loads(row["suggestions"]) == []
                assert row["activity_digest"] == ""
                assert row["cleared_message_id"] == row["revision"] == 0
                assert row["cleared_at"] is None
                assert row["created_at"] == row["updated_at"]
                # Onboarding may initialize memory before the owner's first job.
                await conn.execute(
                    "INSERT INTO agent_user_memories (user_id, portrait) VALUES ('bob', '{}'::jsonb)"
                )
                for column in ("revision", "cleared_message_id"):
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with conn.transaction():
                            await conn.execute(
                                f"UPDATE agent_user_memories SET {column} = -1 WHERE user_id = 'alice'"
                            )
                with pytest.raises(asyncpg.UniqueViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO agent_user_memories (user_id, portrait) "
                            "VALUES ('alice', '{}'::jsonb)"
                        )
                assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memories") == 2
                assert await conn.fetchval(
                    "SELECT task_prompt FROM agent_jobs WHERE id = $1", job["id"]
                ) == ("I prefer readable API tests.")
            downgrade_to(config, "0046_job_claim_retry")
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT to_regclass('agent_user_memories')") is None
                for table, original in sources.items():
                    assert [
                        dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")
                    ] == original
    finally:
        apply_migrations(config)


async def test_memory_overrides_migration_preserves_reset_fence_and_source_history(store, pool):
    job = await _create_job(store, user_id="alice", task_prompt="I prefer readable API tests.")
    config = _db_config()
    try:
        downgrade_to(config, "0049_host_drain_fence")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait, activity_digest, revision, "
                "cleared_message_id, cleared_at) VALUES ('alice', '{}'::jsonb, 'old-digest', 7, 10, NOW())"
            )
            before = dict(
                await conn.fetchrow("SELECT * FROM agent_user_memories WHERE user_id = 'alice'")
            )
        apply_migrations(config)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_user_memories WHERE user_id = 'alice'")
            assert json.loads(row["overrides"]) == []
            assert row["activity_digest"] == ""
            assert row["revision"] == before["revision"]
            assert row["cleared_message_id"] == before["cleared_message_id"]
            assert row["cleared_at"] == before["cleared_at"]
            assert (
                await conn.fetchval("SELECT task_prompt FROM agent_jobs WHERE id = $1", job["id"])
                == "I prefer readable API tests."
            )
        downgrade_to(config, "0049_host_drain_fence")
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_user_memories WHERE user_id = 'alice'")
            assert "overrides" not in dict(row)
            assert row["cleared_at"] == before["cleared_at"]
    finally:
        apply_migrations(config)
