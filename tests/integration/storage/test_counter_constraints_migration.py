"""Durable authority counters remain nonnegative after the merged migration chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store
from tests.integration.storage.test_snapshot_publish_retry import _capture_settled_workspace

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_authority_counters_reject_negative_values(pool, store) -> None:
    """GitLab generations and publish attempts cannot move below their origin."""
    _, capture, _ = await _capture_settled_workspace(store, "counter-constraints")

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
                capture["snapshot_id"],
            )
