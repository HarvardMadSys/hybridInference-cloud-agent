"""What the database refuses on its own, with no store code in the way.

Before the baseline was rewritten every one of these rows was accepted: the
store checked ownership, fencing and chain membership on its own write paths,
and nothing else did. These probes write the malformed row directly and
expect PostgreSQL to say no — so a new write path, an import script or one
missed check cannot produce a row the readers were never written to handle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest
import pytest_asyncio

# pytest names an imported fixture after the attribute it is bound to, so the
# shared fixture arrives here as ``_shared_store`` and is re-exposed under the
# name the tests ask for.
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_shared_chat,
    store as _shared_store,  # noqa: F401
)

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture(name="store")
async def _store(_shared_store: AgentJobStore) -> AgentJobStore:  # noqa: F811
    """The clean store the job-store tests use, under the same name."""
    return _shared_store


async def _refused(store: AgentJobStore, error: type[Exception], sql: str, *params) -> None:
    async with store._pool.acquire() as conn:
        with pytest.raises(error):
            await conn.execute(sql, *params)


async def test_a_job_belongs_to_an_existing_thread(store: AgentJobStore) -> None:
    job = await _create_job(store)
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "UPDATE agent_jobs SET thread_id = 'athr_missing' WHERE id = $1",
        job["id"],
    )
    await _refused(
        store,
        asyncpg.NotNullViolationError,
        "UPDATE agent_jobs SET thread_id = NULL WHERE id = $1",
        job["id"],
    )


async def test_a_job_is_owned_by_its_threads_owner(store: AgentJobStore) -> None:
    job = await _create_job(store)
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "UPDATE agent_jobs SET user_id = 'someone-else' WHERE id = $1",
        job["id"],
    )


async def test_job_state_is_a_closed_vocabulary(store: AgentJobStore) -> None:
    job = await _create_job(store)
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_jobs SET state = 'publishing' WHERE id = $1",
        job["id"],
    )


async def test_a_jobs_current_attempt_is_one_of_its_own(store: AgentJobStore) -> None:
    first = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim["id"] == first["id"]
    other = await _create_job(store)
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "UPDATE agent_jobs SET current_attempt_id = $2 WHERE id = $1",
        other["id"],
        claim["attempt_id"],
    )


async def test_an_event_is_filed_under_its_attempts_job(store: AgentJobStore) -> None:
    first = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim["id"] == first["id"]
    other = await _create_job(store)
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type) VALUES ($1, $2, 1, 'x')",
        other["id"],
        claim["attempt_id"],
    )


async def test_an_attempt_keeps_a_valid_status_and_a_positive_generation(
    store: AgentJobStore,
) -> None:
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_attempts SET status = 'audit-invalid' WHERE id = $1",
        claim["attempt_id"],
    )
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_attempts SET lease_generation = -1 WHERE id = $1",
        claim["attempt_id"],
    )
    # Leaving ``running`` without a finish time, or finishing while running,
    # is the same lie told two ways.
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_attempts SET status = 'superseded' WHERE id = $1",
        claim["attempt_id"],
    )


async def test_a_membership_cannot_cross_tenants(store: AgentJobStore) -> None:
    _job, _claim_row, sandbox = await _reserve_shared_chat(store)
    foreign = await _create_job(store, user_id="someone-else")
    # Either owner column is wrong for one of the two parents.
    for owner in ("someone-else", "user-1"):
        await _refused(
            store,
            asyncpg.ForeignKeyViolationError,
            "INSERT INTO agent_thread_sandbox_memberships (thread_id, user_id, sandbox_id) "
            "VALUES ($1, $2, $3)",
            foreign["thread_id"],
            owner,
            sandbox["sandbox_id"],
        )


async def test_a_membership_names_only_its_own_threads_jobs_and_attempts(
    store: AgentJobStore,
) -> None:
    job, _claim_row, _sandbox = await _reserve_shared_chat(store)
    other = await _create_job(store)
    other_claim = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
    assert other_claim["id"] == other["id"]
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "UPDATE agent_thread_sandbox_memberships SET active_attempt_id = $2 WHERE thread_id = $1",
        job["thread_id"],
        other_claim["attempt_id"],
    )
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "UPDATE agent_thread_sandbox_memberships SET last_settled_job_id = $2 WHERE thread_id = $1",
        job["thread_id"],
        other["id"],
    )


async def test_a_hot_residency_carries_a_lease(store: AgentJobStore) -> None:
    job, _claim_row, _sandbox = await _reserve_shared_chat(store)
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_thread_sandbox_memberships SET residency_status = 'hot' WHERE thread_id = $1",
        job["thread_id"],
    )


async def test_a_snapshot_includes_only_existing_jobs(store: AgentJobStore) -> None:
    job, _claim_row, sandbox = await _reserve_shared_chat(store)
    async with store._pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                snapshot_id = await conn.fetchval(
                    "INSERT INTO agent_sandbox_snapshots (sandbox_id, workspace_epoch, "
                    "anchor_job_id, base_sha) VALUES ($1, 1, $2, 'base') RETURNING id",
                    sandbox["sandbox_id"],
                    job["id"],
                )
                await conn.execute(
                    "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, $2)",
                    snapshot_id,
                    job["id"],
                )
                await conn.execute(
                    "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, 'ajob_missing')",
                    snapshot_id,
                )


async def test_a_snapshots_anchor_is_one_of_its_included_jobs(store: AgentJobStore) -> None:
    """The deferred key is checked at commit, so the whole transaction is refused."""
    job, _claim_row, sandbox = await _reserve_shared_chat(store)
    async with store._pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO agent_sandbox_snapshots (sandbox_id, workspace_epoch, "
                    "anchor_job_id, base_sha) VALUES ($1, 1, $2, 'base')",
                    sandbox["sandbox_id"],
                    job["id"],
                )
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_sandbox_snapshots") == 0


async def test_a_succeeded_job_carries_its_native_turn(store: AgentJobStore) -> None:
    job = await _create_job(store)
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_jobs SET state = 'succeeded' WHERE id = $1",
        job["id"],
    )
    # A turn that settled its native context and then failed keeps the
    # ordinal: the constraint is one-directional on purpose.
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', native_turn_no = 1 WHERE id = $1",
            job["id"],
        )


async def test_a_fork_copy_is_terminal(store: AgentJobStore) -> None:
    source = await _create_job(store)
    copy = await _create_job(store)
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_jobs SET fork_source_job_id = $2 WHERE id = $1",
        copy["id"],
        source["id"],
    )


async def test_a_message_and_a_parent_stay_in_the_jobs_thread(store: AgentJobStore) -> None:
    first = await _create_job(store)
    second = await _create_job(store)
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "INSERT INTO agent_thread_messages (thread_id, job_id, role, content) "
        "VALUES ($1, $2, 'assistant', 'x')",
        first["thread_id"],
        second["id"],
    )
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "UPDATE agent_jobs SET parent_job_id = $2, turn_no = 2 WHERE id = $1",
        second["id"],
        first["id"],
    )
    # And a root turn is exactly the one without a parent.
    await _refused(
        store,
        asyncpg.CheckViolationError,
        "UPDATE agent_jobs SET turn_no = 2 WHERE id = $1",
        second["id"],
    )


async def test_resource_rows_belong_to_their_attempts_job(store: AgentJobStore) -> None:
    first = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim["id"] == first["id"]
    other = await _create_job(store)
    await _refused(
        store,
        asyncpg.ForeignKeyViolationError,
        "INSERT INTO agent_attempt_resources (attempt_id, job_id, sampler_version) "
        "VALUES ($1, $2, 'v1')",
        claim["attempt_id"],
        other["id"],
    )
