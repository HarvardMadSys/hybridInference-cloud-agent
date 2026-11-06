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
- **The Audit log reads it back newest first, filtered in SQL**, by action,
  by kind of actor, by affected user and by target, and paging by ``id`` must
  neither repeat nor skip a row.
- **The kind of actor is derived by the database**, so a row a previous
  release writes is classified like one this release writes.
- **Recording never fails the action**: a row that cannot be written is
  counted instead, and the caller's transaction carries on.
- **A ``requested`` row reads with its final result**, reported by a worker
  or read from the compute operation it names.
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
            await conn.execute("DELETE FROM agent_admin_audit_outcomes")
            await conn.execute("DELETE FROM agent_admin_audit_write_failures")
            await conn.execute("DELETE FROM agent_admin_audit_log")
            await conn.execute(
                "DELETE FROM agent_compute_operations WHERE starts_with(id, 'op-audit-')"
            )

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


async def test_exact_actions_filter_the_read(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    """A filter is the exact list of actions it shows; ``_`` is not a wildcard."""
    await _record(store, action="agent.runner_host.drain")
    await _record(store, action="agent.runner_host.set_settings")
    await _record(store, action="agent.compute.server.add")
    await _record(store, action="agent.runnerXhost.drain")

    assert await _actions(log, actions=("agent.runner_host.drain", "agent.compute.server.add")) == [
        "agent.compute.server.add",
        "agent.runner_host.drain",
    ]


@pytest.mark.parametrize(
    ("actor_id", "actor_type"),
    [
        ("admin:01KQ", "admin"),
        ("self:01M6", "user"),
        ("anonymous", "anonymous"),
        ("password-reset", "anonymous"),
        ("admin-token", "break_glass"),
        ("deployment-bootstrap", "break_glass"),
        ("first-run-setup", "break_glass"),
        ("operator:realtmxi", "break_glass"),
        ("admin:staging-cleanup", "service"),
        ("codex-ops-stale-cleanup", "service"),
        ("service:deploy-pipeline", "service"),
        ("sandbox-deletion-worker", "system"),
        ("signup-policy", "system"),
        ("admin-emails", "system"),
    ],
)
async def test_the_database_names_the_kind_of_actor(
    store: PostgresAuditRepository, pool, actor_id: str, actor_type: str
) -> None:
    row_id = await _record(store, actor_id=actor_id)
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT actor_type FROM agent_admin_audit_log WHERE id = $1", row_id
            )
            == actor_type
        )


async def test_a_row_the_previous_release_writes_is_classified_too(pool) -> None:
    """Its insert names only the original columns."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_admin_audit_log (action, actor_id, admin_ip, details, success) "
            "VALUES ('agent.runner_host.drain', 'admin-token', 'x', NULL, TRUE)"
        )
        assert await conn.fetchval("SELECT actor_type FROM agent_admin_audit_log") == "break_glass"


async def test_people_users_and_targets_narrow_the_read(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    await _record(
        store, action="agent.runner_host.drain", actor_id="admin-token", target_user_id=None
    )
    await _record(
        store,
        action="agent.sandbox.delete.requested",
        actor_id="admin:u-1",
        target_user_id="owner-1",
        details={"sandbox_id": "asbx_1", "request_id": "req-1"},
    )
    await _record(
        store, action="agent.local_account.signup", actor_id="anonymous", target_user_id="new-1"
    )
    await _record(
        store, action="agent.local_account.approve", actor_id="admin:u-1", target_user_id="new-1"
    )

    assert await _actions(log, actor_types=("admin", "break_glass")) == [
        "agent.local_account.approve",
        "agent.sandbox.delete.requested",
        "agent.runner_host.drain",
    ]
    assert await _actions(log, subject_user_id="new-1") == [
        "agent.local_account.approve",
        "agent.local_account.signup",
    ]
    assert await _actions(log, target=("sandbox", "asbx_1")) == ["agent.sandbox.delete.requested"]
    assert await _actions(log, target=("host", "runner-a")) == ["agent.runner_host.drain"]


async def test_a_row_stores_what_it_acted_on_and_its_outcome(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository
) -> None:
    await _record(
        store,
        action="agent.sandbox.delete.requested",
        target_user_id="owner-1",
        details={"sandbox_id": "asbx_1", "request_id": "req-1"},
    )
    await _record(
        store, action="agent.profiling.start", details={"outcome": "refused"}, success=False
    )
    await _record(
        store, action="agent.local_account.reject", target_user_id="new-1", reason="not a member"
    )

    reject, start, delete = await log.list_admin_actions(limit=10)

    assert (delete["target_type"], delete["target_id"]) == ("sandbox", "asbx_1")
    assert (delete["outcome"], delete["correlation_id"]) == ("requested", "req-1")
    assert delete["result"] is None
    assert (start["target_type"], start["outcome"]) == ("measurement", "refused")
    assert (reject["target_type"], reject["target_id"], reject["reason"]) == (
        "account",
        "new-1",
        "not a member",
    )


async def test_a_name_survives_the_account_that_had_it(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository, pool
) -> None:
    """An erased account's last recorded name labels the rows written after it."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_admin_audit_log "
            "(action, actor_id, admin_ip, target_user_id, success, subject_label) "
            "VALUES ('agent.local_account.delete', 'admin:u-1', 'x', 'gone-1', TRUE, 'alice')"
        )
    await _record(store, action="agent.local_account.erase", target_user_id="gone-1", details={})

    erase = (await log.list_admin_actions(limit=1))[0]
    assert (erase["subject_label"], erase["target_label"]) == ("alice", "alice")


async def test_a_write_that_fails_is_counted_and_the_transaction_carries_on(pool) -> None:
    from cloud_agent.storage._sql.admin_audit import insert_audit_row

    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE agent_admin_audit_log ADD CONSTRAINT reject_audit_test "
            "CHECK (action <> 'agent.runner_host.forget')"
        )
        try:
            async with conn.transaction():
                assert (
                    await insert_audit_row(
                        conn,
                        action="agent.runner_host.forget",
                        actor_id="admin:u-1",
                        admin_ip="x",
                        details={"host": "h"},
                    )
                    is None
                )
                # The caller's transaction is intact and commits its own work.
                assert await conn.fetchval("SELECT 1") == 1
        finally:
            await conn.execute(
                "ALTER TABLE agent_admin_audit_log DROP CONSTRAINT reject_audit_test"
            )
        failure = await conn.fetchrow(
            "SELECT action, actor_id, error FROM agent_admin_audit_write_failures"
        )
    assert tuple(failure) == ("agent.runner_host.forget", "admin:u-1", "CheckViolationError")


async def test_recent_write_failures_are_counted(log: PostgresAuditLogRepository, pool) -> None:
    from datetime import UTC, datetime, timedelta

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_admin_audit_write_failures (action, error, created_at) VALUES "
            "('a', 'E', NOW() - INTERVAL '8 days'), ('b', 'E', NOW() - INTERVAL '1 day'), ('c', 'E', NOW())"
        )
    count, latest = await log.write_failures_since(datetime.now(UTC) - timedelta(days=7))
    assert count == 2 and latest is not None


async def test_a_requested_row_reads_with_its_reported_result(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository, pool
) -> None:
    from cloud_agent.storage._sql.admin_audit import report_final_outcome

    requested = ("agent.sandbox.delete.requested",)
    await _record(
        store,
        action="agent.sandbox.delete.requested",
        details={"sandbox_id": "asbx_1", "request_id": "req-1"},
    )
    async with pool.acquire() as conn:
        await report_final_outcome(
            conn,
            requested_actions=requested,
            correlation_id="req-1",
            outcome="failed",
            reported_by="w",
        )
        assert (await log.list_admin_actions(limit=1))[0]["result"] == "failed"
        # Reported again: the newer word wins.
        await report_final_outcome(
            conn,
            requested_actions=requested,
            correlation_id="req-1",
            outcome="applied",
            reported_by="w",
        )
        # A request nobody recorded is left alone.
        await report_final_outcome(
            conn,
            requested_actions=requested,
            correlation_id="req-x",
            outcome="applied",
            reported_by="w",
        )
        assert await conn.fetchval("SELECT count(*) FROM agent_admin_audit_outcomes") == 1
    row = (await log.list_admin_actions(limit=1))[0]
    assert row["result"] == "applied" and row["result_at"] is not None


async def test_a_compute_request_reads_with_its_operations_state(
    store: PostgresAuditRepository, log: PostgresAuditLogRepository, pool
) -> None:
    """The operation's own state is the result; nothing copies it."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_compute_operations (id, request_id, fingerprint, kind, state, completed_at) "
            "VALUES ('op-audit-1', 'r1', 'f', 'add', 'succeeded', NOW()), "
            "('op-audit-2', 'r2', 'f', 'add', 'running', NULL)"
        )
    await _record(store, action="agent.compute.server.add", details={"operation_id": "op-audit-1"})
    await _record(store, action="agent.compute.server.add", details={"operation_id": "op-audit-2"})

    running, done = await log.list_admin_actions(limit=10)

    assert (done["outcome"], done["result"]) == ("requested", "applied")
    assert done["result_at"] is not None
    assert (running["result"], running["result_at"]) == (None, None)
