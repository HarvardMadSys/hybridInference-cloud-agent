"""Durable authority counters remain nonnegative after the merged migration chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _reserve_shared_chat,
    pool as pool,
    store as store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_authority_counters_reject_negative_values(pool, store) -> None:
    """GitLab generations and publish attempts cannot move below their origin."""
    job, _claim, sandbox = await _reserve_shared_chat(store)
    async with pool.acquire() as conn, conn.transaction():
        # Written by no release now (#1103), but the table stands until a later one.
        snapshot_id = await conn.fetchval(
            "INSERT INTO agent_sandbox_snapshots "
            "(sandbox_id, workspace_epoch, anchor_job_id, base_sha) "
            "VALUES ($1, 1, $2, $3) RETURNING id",
            sandbox["sandbox_id"],
            job["id"],
            "a" * 40,
        )
        await conn.execute(
            "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, $2)",
            snapshot_id,
            job["id"],
        )

    async with pool.acquire() as conn:
        connection_id = await conn.fetchval(
            """
            INSERT INTO agent_gitlab_connections (
                user_id, external_user_id, username, access_token_ciphertext,
                refresh_token_ciphertext, expires_at
            )
            VALUES ($1, 1, 'counter-constraints', 'access', 'refresh', $2)
            RETURNING id
            """,
            "counter-constraints-owner",
            datetime.now(UTC) + timedelta(hours=1),
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_gitlab_connections SET refresh_generation = -1 WHERE id = $1",
                connection_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_sandbox_snapshots SET publish_attempt_count = -1 WHERE id = $1",
                snapshot_id,
            )
