"""Claim retry eligibility upgrades over the merged upstream schema and data."""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_claim_retry_upgrade_preserves_existing_jobs_and_round_trips(store, pool):
    job = await _create_job(store)
    config = _db_config()
    columns = "id, thread_id, user_id, state, task_prompt, metadata, current_attempt_id"
    async with pool.acquire() as conn:
        original = dict(
            await conn.fetchrow(f"SELECT {columns} FROM agent_jobs WHERE id=$1", job["id"])
        )
    try:
        downgrade_to(config, "0045_admin_sandbox_deletion")
        for _ in range(2):
            apply_migrations(config)
            async with pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT version_num FROM alembic_version")
                    == "0046_job_claim_retry"
                )
                current = dict(
                    await conn.fetchrow(f"SELECT {columns} FROM agent_jobs WHERE id=$1", job["id"])
                )
                assert current == original
                assert (
                    await conn.fetchval(
                        "SELECT claim_retry_at FROM agent_jobs WHERE id=$1", job["id"]
                    )
                    is None
                )
                column = await conn.fetchrow(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='agent_jobs' AND column_name='claim_retry_at'"
                )
                assert dict(column) == {
                    "data_type": "timestamp with time zone",
                    "is_nullable": "YES",
                }
            downgrade_to(config, "0045_admin_sandbox_deletion")
            async with pool.acquire() as conn:
                assert (
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' "
                        "AND table_name='agent_jobs' AND column_name='claim_retry_at'"
                    )
                    == 0
                )
                assert (
                    dict(
                        await conn.fetchrow(
                            f"SELECT {columns} FROM agent_jobs WHERE id=$1", job["id"]
                        )
                    )
                    == original
                )
    finally:
        apply_migrations(config)
