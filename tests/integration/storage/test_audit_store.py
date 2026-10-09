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
- **The Audit log reads it back newest first, filtered in SQL.** Action names
  hold ``_``, which ``LIKE`` reads as a wildcard, and paging by ``id`` must
  neither repeat nor skip a row.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from tests.support.migrations import apply_migrations

from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.audit import PostgresAuditRepository
from cloud_agent.storage.repositories.audit_log import PostgresAuditLogRepository

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


@pytest_asyncio.fixture
async def log(pool):
    """The trail read back, on the repository the Audit log page uses."""
    return PostgresAuditLogRepository(pool)


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


async def _actions(log: PostgresAuditLogRepository, **query) -> list[str]:
    rows = await log.list_admin_actions(limit=query.pop("limit", 50), **query)
    return [row["action"] for row in rows]


async def test_the_trail_reads_back_newest_first_with_decoded_details(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    """The page shows the latest change at the top, details as an object."""
    await _record(store, action="agent.runner_host.drain")
    await _record(store, action="agent.runner_host.undrain", details={"host": "runner-b"})

    rows = await log.list_admin_actions(limit=10)

    assert [row["action"] for row in rows] == [
        "agent.runner_host.undrain",
        "agent.runner_host.drain",
    ]
    assert rows[0]["details"] == {"host": "runner-b"}
    assert rows[0]["actor_id"] == _ACTOR
    assert rows[0]["created_at"] is not None


async def test_pages_follow_one_another_without_repeating_a_row(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    """``before_id`` continues exactly where the previous page ended."""
    ids = [await _record(store, details={"n": n}) for n in range(5)]

    first = await log.list_admin_actions(limit=2)
    second = await log.list_admin_actions(limit=2, before_id=first[-1]["id"])
    third = await log.list_admin_actions(limit=2, before_id=second[-1]["id"])

    seen = [row["id"] for row in first + second + third]
    assert seen == sorted(ids, reverse=True)


async def test_prefixes_and_exact_names_filter_and_underscore_is_literal(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    """A prefix keeps its family, an exact name only itself, ``_`` matches ``_``."""
    await _record(store, action="agent.runner_host.drain")
    await _record(store, action="agent.runner_host.set_settings")
    await _record(store, action="agent.compute.server.add")
    await _record(store, action="agent.profiling.start")
    # Would match ``agent.runner_host.%`` if ``_`` were the wildcard it is in LIKE.
    await _record(store, action="agent.runnerXhost.drain")

    assert await _actions(log, actions=("agent.runner_host.",)) == [
        "agent.runner_host.set_settings",
        "agent.runner_host.drain",
    ]
    assert await _actions(
        log,
        actions=("agent.runner_host.", "agent.compute."),
        excluded_actions=("agent.runner_host.set_settings",),
    ) == ["agent.compute.server.add", "agent.runner_host.drain"]
    assert await _actions(log, actions=("agent.runner_host.set_settings",)) == [
        "agent.runner_host.set_settings"
    ]


async def test_excluded_actions_stay_out_of_an_unfiltered_read(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    """Polling reads written by older releases never reach the page."""
    await _record(store, action="agent.compute.servers.list")
    await _record(store, action="agent.runner_host.drain")
    await _record(store, action="agent.compute.servers.list")

    assert await _actions(log, excluded_actions=("agent.compute.servers.list",)) == [
        "agent.runner_host.drain"
    ]
