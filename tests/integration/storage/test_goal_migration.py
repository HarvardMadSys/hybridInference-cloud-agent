"""Upgrading a goal the previous release wrote into the three-state one.

``0018`` has to cope with the data ``0017`` left behind, not only with a fresh
database: a ``blocked`` state that was really a reason, a ``complete`` row with
no basis recorded, and a scheduling obligation that did not exist yet. These
cases build that shape and upgrade over it, and they leave the shared database
at head whatever the revision makes of the rows.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: The message a ``0017`` row stopped on, kept verbatim as the pause detail.
BLOCK_MESSAGE = "the goal used its whole automatic round budget"


async def _rebuild_at_0017(config) -> None:
    """Build the shape ``0017`` leaves, from an empty database.

    Downgrading across ``0018`` is the other way to get here, and it is what the
    round-trip case at the end uses. This path exists because the cases below
    are about *upgrading*: the previous release's schema is their input, and how
    it was built is not what they are testing.
    """
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
    finally:
        await conn.close()
    apply_migrations(config, "0019_conversation_goal")


async def _reset_to_head(config) -> None:
    """Rebuild the shared database at head, whatever state it was left in.

    A revision that cannot carry a round trip is exactly what the case at the
    end of this file exists to find, and it leaves the database somewhere no
    later test can use. Rebuilding is the only repair that does not depend on
    the very path under test.
    """
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
    finally:
        await conn.close()
    apply_migrations(config, "head")


async def _upgrade_to_head(config, *, thread_id: str) -> None:
    """Upgrade, and leave the database at head even when the revision refuses.

    A revision that cannot carry the previous release's shape forward is
    exactly what these cases exist to find, so the refusal has to fail *this*
    test rather than leave every later one stuck on the old schema.
    """
    try:
        apply_migrations(config, "head")
    except Exception:
        conn = await asyncpg.connect(**config)
        try:
            # The row the revision could not carry forward is the only thing
            # between the database and head; take its goal away and retry.
            await conn.execute(
                "UPDATE agent_threads SET goal_objective = NULL, goal_state = NULL, "
                "goal_max_rounds = NULL WHERE id = $1",
                thread_id,
            )
        finally:
            await conn.close()
        apply_migrations(config, "head")
        raise


async def _seed_0017_goal(
    config,
    *,
    goal_state: str,
    goal_revision: int = 7,
    blocked_code: str | None = None,
    blocked_message: str | None = None,
    with_succeeded_turn: bool = False,
) -> tuple[str, str | None]:
    """One conversation as ``0017`` held it, with the goal the caller describes."""
    thread_id = f"athr_{uuid.uuid4().hex}"
    job_id = f"ajob_{uuid.uuid4().hex}" if with_succeeded_turn else None
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute(
            """
            INSERT INTO agent_threads
                (id, user_id, repo, title, title_source, setup_script, execution_profile,
                 goal_objective, goal_state, goal_max_rounds, goal_revision,
                 goal_blocked_code, goal_blocked_message)
            VALUES ($1, 'user-1', 'owner/name', 'ship it', 'fallback', NULL, 'trusted-v1',
                    'ship it', $2, 5, $3, $4, $5)
            """,
            thread_id,
            goal_state,
            goal_revision,
            blocked_code,
            blocked_message,
        )
        if job_id is not None:
            await conn.execute(
                """
                INSERT INTO agent_jobs
                    (id, thread_id, user_id, turn_no, task_prompt, runtime, model,
                     state, native_turn_no)
                VALUES ($1, $2, 'user-1', 1, 'fix it', 'claude-code', 'glm-5.1',
                        'succeeded', 1)
                """,
                job_id,
                thread_id,
            )
    finally:
        await conn.close()
    return thread_id, job_id


@pytest_asyncio.fixture
async def at_0017():
    """A database built to ``0017``'s shape, restored to head afterwards."""
    config = _db_config()
    await _rebuild_at_0017(config)
    try:
        yield config
    finally:
        await _reset_to_head(config)


@pytest.mark.parametrize(
    ("blocked_code", "reason"),
    [("round-limit", "round_limit"), ("model-reported", "needs_input")],
)
async def test_a_blocked_goal_becomes_paused_with_the_message_it_stopped_on(
    at_0017, blocked_code, reason
):
    """``blocked`` was never a state, it was a reason — and it keeps saying which."""
    thread_id, _ = await _seed_0017_goal(
        at_0017,
        goal_state="blocked",
        goal_revision=7,
        blocked_code=blocked_code,
        blocked_message=BLOCK_MESSAGE,
    )

    await _upgrade_to_head(at_0017, thread_id=thread_id)

    conn = await asyncpg.connect(**at_0017)
    try:
        row = dict(
            await conn.fetchrow(
                "SELECT goal_id, goal_state, goal_pause_reason, goal_pause_detail, "
                "goal_objective, goal_max_rounds, goal_revision "
                "FROM agent_threads WHERE id = $1",
                thread_id,
            )
        )
    finally:
        await conn.close()

    assert row["goal_state"] == "paused"
    assert row["goal_pause_reason"] == reason
    assert row["goal_pause_detail"] == BLOCK_MESSAGE
    # The identity is minted for the objective that already existed, and the
    # control version the previous release was at is not rewritten.
    assert row["goal_id"] is not None
    assert row["goal_revision"] == 7
    assert (row["goal_objective"], row["goal_max_rounds"]) == ("ship it", 5)


async def test_a_completed_goal_keeps_the_turn_that_earned_it(at_0017):
    """A first-version completion was completed because the model said so."""
    thread_id, job_id = await _seed_0017_goal(
        at_0017, goal_state="complete", goal_revision=4, with_succeeded_turn=True
    )

    await _upgrade_to_head(at_0017, thread_id=thread_id)

    conn = await asyncpg.connect(**at_0017)
    try:
        row = dict(
            await conn.fetchrow(
                "SELECT goal_id, goal_state, goal_completion_basis, goal_completed_by_job_id "
                "FROM agent_threads WHERE id = $1",
                thread_id,
            )
        )
    finally:
        await conn.close()

    assert row["goal_state"] == "complete"
    assert row["goal_completion_basis"] == "model_reported"
    assert row["goal_completed_by_job_id"] == job_id
    assert row["goal_id"] is not None


async def test_a_completed_goal_with_no_turn_to_name_is_judged_again(at_0017):
    """Nothing is backfilled that cannot be proven, including a completion."""
    thread_id, _ = await _seed_0017_goal(at_0017, goal_state="complete", goal_revision=4)

    await _upgrade_to_head(at_0017, thread_id=thread_id)

    conn = await asyncpg.connect(**at_0017)
    try:
        row = dict(
            await conn.fetchrow(
                "SELECT goal_id, goal_state, goal_completion_basis, goal_completed_by_job_id "
                "FROM agent_threads WHERE id = $1",
                thread_id,
            )
        )
    finally:
        await conn.close()

    # No turn ever succeeded here, so the old release cannot have completed on
    # one, and the goal goes back to being judged.
    assert row["goal_state"] == "active"
    assert (row["goal_completion_basis"], row["goal_completed_by_job_id"]) == (None, None)
    assert row["goal_id"] is not None


async def test_the_upgraded_schema_has_the_wakeup_queue_and_its_partial_indexes(store):
    """The scheduling obligation is a row, and the two hot lookups are partial."""
    await _create_job(store)
    config = _db_config()

    conn = await asyncpg.connect(**config)
    try:
        indexes = {
            row["indexname"]: row["indexdef"]
            for row in await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'agent_goal_wakeups'"
            )
        }
        trigger_check = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'agent_goal_wakeups'::regclass AND conname = "
            "'agent_goal_wakeups_trigger_check'"
        )
    finally:
        await conn.close()

    # One live wakeup per event, and the consumer's "what is due" query.
    assert "WHERE (state = 'pending'::text)" in indexes["uq_agent_goal_wakeups_event"]
    assert "(thread_id, trigger, trigger_ref)" in indexes["uq_agent_goal_wakeups_event"]
    assert "WHERE (state = 'pending'::text)" in indexes["idx_agent_goal_wakeups_due"]
    assert "(not_before)" in indexes["idx_agent_goal_wakeups_due"]
    # A wakeup is an obligation, never an authorization: only the two events
    # that can owe one may name a trigger.
    assert "'owner'::text" in trigger_check and "'finish'::text" in trigger_check


async def test_the_old_blocked_columns_are_gone(store):
    """``blocked`` is a pause reason now; nothing writes a streak any more."""
    await _create_job(store)

    conn = await asyncpg.connect(**_db_config())
    try:
        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'agent_threads'"
            )
        }
    finally:
        await conn.close()

    assert columns & {"goal_blocked_code", "goal_blocked_message", "goal_blocked_streak"} == set()
    assert {"goal_id", "goal_pause_detail", "goal_completion_basis"} <= columns
    assert "goal_repairs_used" in columns


async def test_the_previous_releases_conversation_round_trips_with_no_goal(store):
    """A conversation the previous release wrote upgrades with no goal at all."""
    job = await _create_job(store)
    config = _db_config()
    try:
        await _round_trip(config, job)
    finally:
        await _reset_to_head(config)


async def _round_trip(config, job) -> None:
    """Walk a conversation down to the pre-goal shape and back up again."""
    downgrade_to(config, "0016_job_attachments")
    conn = await asyncpg.connect(**config)
    try:
        # A turn written by the release before either goal revision: no goal
        # columns exist yet, and the turn has no source of its own.
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            job["id"],
        )
        before = dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"]))
        assert "turn_source" not in before
    finally:
        await conn.close()

    apply_migrations(config, "head")
    conn = await asyncpg.connect(**config)
    try:
        after = dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"]))
        thread = dict(
            await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"])
        )
    finally:
        await conn.close()

    # Every new column takes its default, and the shape constraint's promise —
    # no objective without a state, a budget and an identity — holds for the
    # row the previous release wrote.
    assert after["turn_source"] == "human"
    assert after["goal_round_kind"] is None and after["goal_report_parse"] is None
    assert thread["goal_objective"] is None
    assert thread["goal_state"] is None and thread["goal_max_rounds"] is None
    assert thread["goal_id"] is None and thread["goal_revision"] == 0
    assert thread["goal_repairs_used"] == 0
    # Rounds spent are derived from the rounds that started, so there is no
    # counter column to have a default.
    assert "goal_rounds_used" not in thread

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
