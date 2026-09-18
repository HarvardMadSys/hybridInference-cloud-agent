"""A conversation that predates goals upgrades with no goal, and rolls back cleanly."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_upgrade_leaves_every_existing_conversation_goalless(store):
    job = await _create_job(store)
    config = _db_config()
    downgrade_to(config, "0016_job_attachments")
    conn = await asyncpg.connect(**config)
    try:
        # A turn written by the previous release: no goal columns exist yet.
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            job["id"],
        )
        before = dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"]))
        assert "turn_source" not in before
    finally:
        await conn.close()

    apply_migrations(config, "0017_conversation_goal")
    conn = await asyncpg.connect(**config)
    try:
        after = dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"]))
        thread = dict(
            await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"])
        )
    finally:
        await conn.close()

    # Every new column takes its default, and the shape constraint's promise —
    # no objective without a state and a budget — holds for the old row.
    assert after["turn_source"] == "human"
    assert after["goal_round"] is None and after["goal_report"] is None
    assert thread["goal_objective"] is None
    assert thread["goal_state"] is None and thread["goal_max_rounds"] is None
    assert thread["goal_revision"] == 0
    assert thread["goal_rounds_used"] == 0

    # A goal written under the new revision is rejected when half of it is
    # missing: the constraint is what stops a conversation that has an
    # objective nothing can run.
    conn = await asyncpg.connect(**config)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_threads SET goal_objective = 'ship it' WHERE id = $1",
                job["thread_id"],
            )
    finally:
        await conn.close()

    # Rolling back removes the goal columns and leaves the turn as it was.
    downgrade_to(config, "0016_job_attachments")
    conn = await asyncpg.connect(**config)
    try:
        rolled_back = dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"]))
        assert rolled_back == before
    finally:
        await conn.close()
    apply_migrations(config, "head")
