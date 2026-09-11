"""Pending turns survive the waiting-to-queued upgrade and safe rollback."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _child(conn, parent_id, child_id, state):
    await conn.execute(
        """
        INSERT INTO agent_jobs (
            id, thread_id, user_id, parent_job_id, turn_no, task_prompt,
            runtime, model, state
        )
        SELECT $2, thread_id, user_id, id, turn_no + 1, 'Follow-up prompt',
               runtime, model, $3
        FROM agent_jobs WHERE id = $1
        """,
        parent_id,
        child_id,
        state,
    )


async def test_upgrade_preserves_pending_jobs_and_rollback_restores_dependency_states(store):
    first = await _create_job(store)
    ready_parent = await _create_job(store)
    lost_parent = await _create_job(store)
    config = _db_config()
    downgrade_to(config, "0004_residency_on_membership")
    conn = await asyncpg.connect(**config)
    try:
        await _child(conn, first["id"], "pending-second", "waiting")
        await _child(conn, "pending-second", "pending-third", "waiting")
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed' WHERE id = $1", ready_parent["id"]
        )
        await _child(conn, ready_parent["id"], "ready-child", "queued")
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            lost_parent["id"],
        )
        await _child(conn, lost_parent["id"], "lost-context-child", "waiting")
        before = [dict(row) for row in await conn.fetch("SELECT * FROM agent_jobs ORDER BY id")]
    finally:
        await conn.close()

    apply_migrations(config)
    conn = await asyncpg.connect(**config)
    try:
        after = [dict(row) for row in await conn.fetch("SELECT * FROM agent_jobs ORDER BY id")]
        assert after == [
            {**row, "state": "queued" if row["state"] == "waiting" else row["state"]}
            for row in before
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_jobs SET state = 'waiting' WHERE id = 'pending-second'"
            )
    finally:
        await conn.close()

    downgrade_to(config, "0004_residency_on_membership")
    conn = await asyncpg.connect(**config)
    try:
        restored = [dict(row) for row in await conn.fetch("SELECT * FROM agent_jobs ORDER BY id")]
        assert restored == before
    finally:
        await conn.close()
    apply_migrations(config)


async def test_rollback_refuses_a_queue_the_old_scheduler_could_run_out_of_order(store):
    first = await _create_job(store)
    config = _db_config()
    conn = await asyncpg.connect(**config)
    try:
        await _child(conn, first["id"], "cancelled-middle", "cancelled")
        await _child(conn, "cancelled-middle", "blocked-third", "queued")
    finally:
        await conn.close()

    with pytest.raises(Exception, match="Drain queued follow-ups before downgrading"):
        downgrade_to(config, "0004_residency_on_membership")
    conn = await asyncpg.connect(**config)
    try:
        assert (
            await conn.fetchval("SELECT version_num FROM alembic_version")
            == "0005_queued_follow_ups"
        )
        assert (
            await conn.fetchval("SELECT state FROM agent_jobs WHERE id = 'blocked-third'")
            == "queued"
        )
        await conn.execute("UPDATE agent_jobs SET state = 'cancelled' WHERE id = 'blocked-third'")
    finally:
        await conn.close()
    downgrade_to(config, "0004_residency_on_membership")
    apply_migrations(config)
