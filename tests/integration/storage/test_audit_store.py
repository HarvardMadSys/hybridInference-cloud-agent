"""The admin audit trail, against a real database.

The unit tier drives this through fakes, so it pins what the *code* does with
the rows it is handed. Three things only Postgres can settle:

- **The row is append-only and lands as written.** `details` travels as JSONB,
  and the interesting case is the one a fake cannot have: ``None`` must store
  as SQL NULL, not as the JSON literal ``null``, because a reader that
  distinguishes "no detail" from "detail that is null" would otherwise get the
  wrong answer for every row either way.
- **`success=False` is recorded rather than skipped.** A trail that only keeps
  the actions that worked is not a trail.
- **The actor and the target are different columns.** An audit row that blurs
  who acted with whom they acted on cannot answer the question it exists for.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from tests.support.migrations import apply_migrations

from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.audit import PostgresAuditRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_ACTOR = "admin-1"
_TARGET = "user-7"


@pytest_asyncio.fixture
async def pool():
    """A pool over a clean shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(db_config)

    async def _clear() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_admin_audit_log")

    await _clear()
    try:
        yield pool
    finally:
        await _clear()
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    """The capability under test, on the repository that owns it."""
    return PostgresAuditRepository(pool)


async def _record(store: PostgresAuditRepository, **overrides) -> int:
    """Append one row the way an admin route will."""
    payload = {
        "action": "agent.runner_host.drain",
        "actor_id": _ACTOR,
        "admin_ip": "203.0.113.9",
        "target_user_id": _TARGET,
        "details": {"host": "runner-a"},
        "success": True,
    }
    payload.update(overrides)
    return await store.record_admin_action(**payload)


async def test_a_recorded_action_round_trips_field_by_field(
    store: PostgresAuditRepository, pool
) -> None:
    """Every column the caller named comes back as it was given."""
    row_id = await _record(store)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_admin_audit_log WHERE id = $1", row_id)
    assert row is not None
    assert row["action"] == "agent.runner_host.drain"
    assert row["actor_id"] == _ACTOR
    assert row["admin_ip"] == "203.0.113.9"
    assert row["target_user_id"] == _TARGET
    # JSONB comes back as text from a bare fetch; decoded the way the code
    # decodes it, which is what a reader of this column actually gets.
    assert load_json(row["details"]) == {"host": "runner-a"}
    assert row["success"] is True


async def test_no_detail_is_stored_as_absent_rather_than_as_null(
    store: PostgresAuditRepository, pool
) -> None:
    """``None`` must not become the JSON literal ``null`` in a JSONB column."""
    row_id = await _record(store, details=None)

    async with pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT details FROM agent_admin_audit_log WHERE id = $1", row_id
        )
        is_null = await conn.fetchval(
            "SELECT details IS NULL FROM agent_admin_audit_log WHERE id = $1", row_id
        )
    assert stored is None
    assert is_null is True


async def test_a_failed_action_is_recorded_rather_than_skipped(
    store: PostgresAuditRepository, pool
) -> None:
    """A trail that keeps only what worked is not a trail."""
    row_id = await _record(store, success=False)

    async with pool.acquire() as conn:
        success = await conn.fetchval(
            "SELECT success FROM agent_admin_audit_log WHERE id = $1", row_id
        )
    assert success is False


async def test_the_actor_and_the_target_are_separate_columns(
    store: PostgresAuditRepository, pool
) -> None:
    """An action taken on nobody records no target, and still names the actor."""
    row_id = await _record(store, target_user_id=None)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_admin_audit_log WHERE id = $1", row_id)
    assert row is not None
    assert row["actor_id"] == _ACTOR
    assert row["target_user_id"] is None


async def test_recording_twice_appends_rather_than_replaces(
    store: PostgresAuditRepository, pool
) -> None:
    """There is no code here that updates or deletes a row, and this pins that."""
    first = await _record(store, action="agent.runner_host.drain")
    second = await _record(store, action="agent.runner_host.forget")

    assert first != second
    async with pool.acquire() as conn:
        actions = await conn.fetch("SELECT action FROM agent_admin_audit_log ORDER BY id")
    assert [row["action"] for row in actions] == [
        "agent.runner_host.drain",
        "agent.runner_host.forget",
    ]
