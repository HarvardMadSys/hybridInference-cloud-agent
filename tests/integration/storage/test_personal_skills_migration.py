"""Personal skills upgrade and rollback preserve pre-existing job data."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_personal_skills_upgrade_and_rollback_preserve_existing_jobs(store):
    await _create_job(store)
    config = _db_config()
    downgrade_to(config, "0012_host_concurrency")
    conn = await asyncpg.connect(**config)
    try:
        before = [dict(row) for row in await conn.fetch("SELECT * FROM agent_jobs ORDER BY id")]
    finally:
        await conn.close()

    apply_migrations(config, "0013_personal_skills")
    conn = await asyncpg.connect(**config)
    try:
        after = [dict(row) for row in await conn.fetch("SELECT * FROM agent_jobs ORDER BY id")]
        assert after == [{**row, "skills": None} for row in before]
        skill_id = await conn.fetchval(
            "INSERT INTO agent_skills (user_id, name, description, instructions, source) "
            "VALUES ('user-1', 'Review', 'Review changes', 'Read the diff.', 'created') "
            "RETURNING id"
        )
        assert skill_id is not None
        # Snapshot and library data are the only data removed by rollback.
        await conn.execute(
            'UPDATE agent_jobs SET skills = \'[{"instructions":"Read the diff."}]\'::jsonb'
        )
    finally:
        await conn.close()

    downgrade_to(config, "0012_host_concurrency")
    conn = await asyncpg.connect(**config)
    try:
        restored = [dict(row) for row in await conn.fetch("SELECT * FROM agent_jobs ORDER BY id")]
        assert restored == before
        assert await conn.fetchval("SELECT to_regclass('agent_skills')") is None
    finally:
        await conn.close()
