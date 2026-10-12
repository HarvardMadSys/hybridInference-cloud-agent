"""User-memory edits upgrade over existing profiles and round-trip safely."""

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

_BEFORE = "0049_host_drain_fence"
_THIS = "0050_user_memory_edits"
_TRAJECTORY_HEAD = "0046_admin_sandbox_trajectory"
_PRESERVED_TABLES = {
    "agent_jobs": "id",
    "agent_threads": "id",
    "agent_thread_messages": "id",
    "agent_user_memories": "user_id",
}


async def test_user_memory_edits_upgrade_constraints_and_round_trip_preserve_sources(store, pool):
    """Edits are owner scoped; rollback removes them without changing source data."""
    job = await _create_job(store, user_id="alice", task_prompt="I prefer readable API tests.")
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        async with pool.acquire() as conn:
            versions = await conn.fetch("SELECT version_num FROM alembic_version")
            assert {row["version_num"] for row in versions} == {
                _TRAJECTORY_HEAD,
                _BEFORE,
            }
            await conn.execute(
                "INSERT INTO agent_user_memories "
                "(user_id, portrait, memories, suggestions, activity_digest, revision) "
                "VALUES ('alice', $1::jsonb, $2::jsonb, $3::jsonb, 'existing-digest', 3), "
                "('bob', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, '', 0)",
                json.dumps({"summary": "Prefers readable API tests."}),
                json.dumps([{"id": "shared-memory", "text": "Keep assertions precise."}]),
                json.dumps([{"text": "Add migration coverage."}]),
            )
            originals = {
                table: [
                    dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY {key}")
                ]
                for table, key in _PRESERVED_TABLES.items()
            }

        for _ in range(2):
            apply_migrations(config, _THIS)
            async with pool.acquire() as conn:
                versions = await conn.fetch("SELECT version_num FROM alembic_version")
                assert {row["version_num"] for row in versions} == {
                    _TRAJECTORY_HEAD,
                    _THIS,
                }
                assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0
                for table, key in _PRESERVED_TABLES.items():
                    assert [
                        dict(row)
                        for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY {key}")
                    ] == originals[table]

                row = await conn.fetchrow(
                    "INSERT INTO agent_user_memory_edits "
                    "(user_id, memory_id, source_job_id, content) "
                    "VALUES ('alice', 'shared-memory', $1, 'Keep API assertions readable.') "
                    "RETURNING *",
                    job["id"],
                )
                assert row["source_job_id"] == job["id"]
                assert row["content"] == "Keep API assertions readable."
                assert row["created_at"] is not None
                assert row["created_at"] == row["updated_at"]
                # The source job ID is a snapshot, not a dependency on a retained job.
                await conn.execute(
                    "INSERT INTO agent_user_memory_edits "
                    "(user_id, memory_id, source_job_id, content) "
                    "VALUES ('bob', 'shared-memory', 'deleted-source-job', '界')"
                )
                tombstone = await conn.fetchrow(
                    "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id) "
                    "VALUES ('alice', 'deleted-memory', $1) RETURNING *",
                    job["id"],
                )
                assert tombstone["content"] is None
                assert tombstone["created_at"] == tombstone["updated_at"]
                with pytest.raises(asyncpg.UniqueViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO agent_user_memory_edits "
                            "(user_id, memory_id, source_job_id, content) "
                            "VALUES ('alice', 'shared-memory', $1, 'Duplicate edit.')",
                            job["id"],
                        )
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO agent_user_memory_edits "
                            "(user_id, memory_id, source_job_id, content) "
                            "VALUES ('missing-owner', 'shared-memory', $1, 'Missing owner.')",
                            job["id"],
                        )
                for column in ("user_id", "memory_id", "source_job_id"):
                    with pytest.raises(asyncpg.NotNullViolationError):
                        async with conn.transaction():
                            await conn.execute(
                                f"UPDATE agent_user_memory_edits SET {column} = NULL "
                                "WHERE user_id = 'alice' AND memory_id = 'shared-memory'"
                            )
                for invalid_content in ("", "界" * 501):
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with conn.transaction():
                            await conn.execute(
                                "UPDATE agent_user_memory_edits SET content = $1 "
                                "WHERE user_id = 'alice' AND memory_id = 'shared-memory'",
                                invalid_content,
                            )
                assert await conn.fetchval(
                    "UPDATE agent_user_memory_edits SET content = $1 "
                    "WHERE user_id = 'alice' AND memory_id = 'shared-memory' RETURNING content",
                    "界" * 500,
                ) == ("界" * 500)

                await conn.execute(
                    "INSERT INTO agent_user_memories (user_id, portrait) "
                    "VALUES ('cascade-owner', '{}'::jsonb)"
                )
                await conn.execute(
                    "INSERT INTO agent_user_memory_edits "
                    "(user_id, memory_id, source_job_id, content) "
                    "VALUES ('cascade-owner', 'shared-memory', $1, 'Cascade with owner.')",
                    job["id"],
                )
                await conn.execute(
                    "DELETE FROM agent_user_memories WHERE user_id = 'cascade-owner'"
                )
                assert (
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM agent_user_memory_edits WHERE user_id = 'cascade-owner'"
                    )
                    == 0
                )
                assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 3

            downgrade_to(config, _BEFORE)
            async with pool.acquire() as conn:
                versions = await conn.fetch("SELECT version_num FROM alembic_version")
                assert {row["version_num"] for row in versions} == {
                    _TRAJECTORY_HEAD,
                    _BEFORE,
                }
                assert await conn.fetchval("SELECT to_regclass('agent_user_memory_edits')") is None
                for table, key in _PRESERVED_TABLES.items():
                    assert [
                        dict(row)
                        for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY {key}")
                    ] == originals[table]
    finally:
        apply_migrations(config)
