"""Upgrading over the data the previous revision wrote, not over an empty one.

``0004_residency_on_membership`` adds a foreign key from a fork's target thread
to ``agent_threads``. Under ``0003`` a fork created its target thread only once
the fork succeeded, so a fork that failed named a thread that was never
created, and the key could not be added over that. These tests build that shape
through the real store, walk the schema back to ``0003``, and upgrade over it:
a revision that only copes with a fresh database is a revision that has not
been tested against the database it will actually run on.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from cloud_agent.storage.assembly import Storage
from tests.integration.storage.test_agent_job_store import _reserve_shared_chat
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_PREVIOUS_REVISION = "0003_thread_level_config"


def _db_config() -> dict[str, object]:
    """The per-worker test database, refusing any name without ``_test_``."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in test_db_name:
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def store():
    """A store over the test database, always left at head for the next test."""
    config = _db_config()
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(config)
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)
    try:
        yield Storage(pool)
    finally:
        # These tests walk the schema backwards. Whatever happened, the shared
        # per-worker database is expected at head by every other test.
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


async def _prepared_fork(store: Storage) -> dict[str, str]:
    """A settled shared chat with one prepared, unfinished native fork."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-shared-one", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(**fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        job_id=job["id"],
        context_status="live",
        native_session_id="native-source",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    operation = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=job["user_id"], idempotency_key="upgrade-fork"
    )
    assert operation is not None and operation.get("error") is None, operation
    return {
        "user_id": str(job["user_id"]),
        "source_thread_id": str(job["thread_id"]),
        "target_thread_id": str(operation["target_thread_id"]),
        "operation_id": str(operation["id"]),
    }


async def test_a_failed_fork_from_the_previous_revision_upgrades(store: Storage):
    """A failure 0003 left behind is a name, not a row; 0004 has to cope."""
    fork = await _prepared_fork(store)
    config = _db_config()
    downgrade_to(config, _PREVIOUS_REVISION)

    # What 0003 left after the reaper failed a stalled fork: the operation
    # still names its target, but neither the target thread nor its membership
    # was ever created. The reaper also released the source membership.
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute(
            "UPDATE agent_session_forks SET state = 'failed', completed_at = NOW(), "
            "last_error = 'coordinator stopped before a receipt was recorded' "
            "WHERE id = $1",
            fork["operation_id"],
        )
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET fork_operation_id = NULL "
            "WHERE thread_id = $1",
            fork["source_thread_id"],
        )
        await conn.execute(
            "DELETE FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            fork["target_thread_id"],
        )
        await conn.execute("DELETE FROM agent_threads WHERE id = $1", fork["target_thread_id"])
    finally:
        await conn.close()

    apply_migrations(config)

    conn = await asyncpg.connect(**config)
    try:
        thread = await conn.fetchrow(
            "SELECT user_id, deleted_at FROM agent_threads WHERE id = $1",
            fork["target_thread_id"],
        )
        member = await conn.fetchrow(
            "SELECT native_state, context_status, resident_state, resident_slot_id, "
            "fork_operation_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            fork["target_thread_id"],
        )
        key = await conn.fetchval(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'fk_agent_session_forks_target_thread_owner'"
        )
    finally:
        await conn.close()

    assert thread is not None, "the operation's target must name a real thread"
    assert thread["user_id"] == fork["user_id"]
    assert thread["deleted_at"] is not None, "a failed fork's target is a tombstone"
    assert member is not None
    assert dict(member) == {
        "native_state": "lost",
        "context_status": "lost",
        "resident_state": "lost",
        "resident_slot_id": None,
        "fork_operation_id": None,
    }
    assert key == 1, "the foreign key the whole revision exists for must be present"


async def test_an_in_flight_fork_refuses_the_upgrade(store: Storage):
    """Tombstoning a fork the coordinator has not finished is not a migration's call."""
    fork = await _prepared_fork(store)
    config = _db_config()
    downgrade_to(config, _PREVIOUS_REVISION)

    try:
        with pytest.raises(Exception, match="preparing or materialized"):
            apply_migrations(config)
    finally:
        # Settle it the way the reaper would, so the fixture can restore head.
        conn = await asyncpg.connect(**config)
        try:
            await conn.execute(
                "UPDATE agent_session_forks SET state = 'failed', completed_at = NOW() "
                "WHERE id = $1",
                fork["operation_id"],
            )
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships SET resident_slot_id = NULL, "
                "native_state = 'lost', context_status = 'lost', resident_state = 'lost', "
                "fork_operation_id = NULL WHERE thread_id = $1",
                fork["target_thread_id"],
            )
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1",
                fork["target_thread_id"],
            )
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships SET fork_operation_id = NULL "
                "WHERE thread_id = $1",
                fork["source_thread_id"],
            )
        finally:
            await conn.close()
