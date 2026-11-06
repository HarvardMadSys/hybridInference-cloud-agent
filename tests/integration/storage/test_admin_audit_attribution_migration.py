"""Revision 0114: the trail's existing rows gain their attribution, and lose only reads.

Rows are written in the shape every earlier release wrote them, under the
previous revision, then the revision runs. It must drop the polling reads and
connection checks and nothing else of an administrator's, classify every
actor, type every target, name the accounts, and turn the sandbox deletion
worker's per-step rows into one final result against each request.
"""

from __future__ import annotations

import json

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.local_accounts import LOCAL_SECRET, make_account
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.accounts import config as account_config

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0113_sandbox_incidents"
_THIS = "0114_admin_audit_attribution"
_WORKER = "sandbox-deletion-worker"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(account_config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    account_config._derived.cache_clear()


async def _legacy(conn, action: str, actor: str, target: str | None, details, success=True) -> None:
    await conn.execute(
        "INSERT INTO agent_admin_audit_log (action, actor_id, admin_ip, target_user_id, details, success) "
        "VALUES ($1, $2, '203.0.113.9', $3, $4::jsonb, $5)",
        action,
        actor,
        target,
        json.dumps(details) if details is not None else None,
        success,
    )


def _deletion(sandbox: str, request: str) -> dict[str, str]:
    return {"sandbox_id": sandbox, "request_id": request, "host": "h"}


async def _seed(conn, admin: str, alice: str) -> None:
    await conn.execute("DELETE FROM agent_admin_audit_log")
    await _legacy(conn, "agent.compute.servers.list", f"admin:{admin}", None, None)
    await _legacy(
        conn, "agent.compute.connection.validate", f"admin:{admin}", None, {"connection_id": "c"}
    )
    await _legacy(conn, "agent.local_account.approve", f"admin:{admin}", alice, {"version": 2})
    await _legacy(conn, "agent.local_account.signup", "anonymous", alice, {"method": "email"})
    await _legacy(conn, "agent.runner_host.drain", "admin-token", None, {"host": "rtx"})
    await _legacy(conn, "agent.runner_host.set_active", "admin-token", None, {"new_host": "rtx2"})
    await _legacy(
        conn, "agent.profiling.start", f"admin:{admin}", None, {"outcome": "refused"}, False
    )
    await _legacy(
        conn, "agent.compute.server.add", f"admin:{admin}", None, {"operation_id": "op-1"}
    )
    await _legacy(conn, "agent.something.retired", "codex-ops-stale-cleanup", None, None)
    # Completed: requested, archived, completed.
    await _legacy(
        conn, "agent.sandbox.delete.requested", f"admin:{admin}", alice, _deletion("s1", "r1")
    )
    await _legacy(conn, "agent.sandbox.delete.archived", _WORKER, alice, _deletion("s1", "r1"))
    await _legacy(conn, "agent.sandbox.delete.completed", _WORKER, alice, _deletion("s1", "r1"))
    # Ran out of retries.
    await _legacy(
        conn, "agent.sandbox.delete.requested", f"admin:{admin}", alice, _deletion("s2", "r2")
    )
    await _legacy(conn, "agent.sandbox.delete.failed", _WORKER, alice, _deletion("s2", "r2"), False)
    await _legacy(
        conn, "agent.sandbox.delete.retry_exhausted", _WORKER, alice, _deletion("s2", "r2")
    )
    # Ran out, then an administrator resumed it: the request failed, the
    # resumption is still running.
    await _legacy(
        conn, "agent.sandbox.delete.requested", f"admin:{admin}", alice, _deletion("s3", "r3")
    )
    await _legacy(
        conn, "agent.sandbox.delete.retry_exhausted", _WORKER, alice, _deletion("s3", "r3")
    )
    await _legacy(
        conn, "agent.sandbox.delete.retry_resumed", f"admin:{admin}", alice, _deletion("s3", "r3")
    )
    # Discarded.
    await _legacy(
        conn, "agent.sandbox.delete.requested", f"admin:{admin}", alice, _deletion("s4", "r4")
    )
    await _legacy(
        conn,
        "agent.sandbox.delete.discarded",
        _WORKER,
        alice,
        _deletion("s4", "r4") | {"host_reclaimed": True},
    )
    # Stopped, resumed, then completed: each row has its own result.
    await _legacy(
        conn, "agent.sandbox.delete.requested", f"admin:{admin}", alice, _deletion("s5", "r5")
    )
    await _legacy(conn, "agent.sandbox.delete.failed", _WORKER, alice, _deletion("s5", "r5"), False)
    await _legacy(
        conn, "agent.sandbox.delete.retry_resumed", f"admin:{admin}", alice, _deletion("s5", "r5")
    )
    await _legacy(
        conn,
        "agent.sandbox.delete.completed",
        _WORKER,
        alice,
        _deletion("s5", "r5") | {"archive_key": "s3://b/k"},
    )
    # A worker row no request names stays.
    await _legacy(conn, "agent.sandbox.delete.completed", _WORKER, alice, _deletion("s9", "r9"))


async def _results(conn) -> dict[tuple[str, str], tuple[str, dict]]:
    """Each request or resumption's final result, by (action suffix, request)."""
    return {
        (row["action"].rsplit(".", 1)[1], row["request_id"]): (
            row["outcome"],
            json.loads(row["details"]),
        )
        for row in await conn.fetch(
            "SELECT l.action, l.correlation_id AS request_id, r.outcome, r.details "
            "FROM agent_admin_audit_outcomes r JOIN agent_admin_audit_log l ON l.id = r.event_id"
        )
    }


async def test_existing_rows_gain_attribution_and_worker_steps_become_results(store) -> None:
    admin = await make_account(store, "juncheng@example.edu", admission="approved", admin=True)
    alice = await make_account(store, "alice@example.edu", admission="approved")
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        await _seed(conn, admin, alice)
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        rows = {
            (row["action"], row["details"] and json.loads(row["details"]).get("request_id")): row
            for row in await conn.fetch("SELECT * FROM agent_admin_audit_log ORDER BY id")
        }
        results = await _results(conn)
    finally:
        await conn.close()

    actions = [action for action, _ in rows]
    assert "agent.compute.servers.list" not in actions
    assert "agent.compute.connection.validate" not in actions
    # Only the unmatched worker row is left of the worker's.
    assert [key for key, row in rows.items() if row["actor_id"] == _WORKER] == [
        ("agent.sandbox.delete.completed", "r9")
    ]
    assert results == {
        ("requested", "r1"): ("applied", {}),
        ("requested", "r2"): ("failed", {}),
        ("requested", "r3"): ("failed", {}),
        ("requested", "r4"): ("applied", {"host_reclaimed": True}),
        ("requested", "r5"): ("failed", {}),
        ("retry_resumed", "r5"): ("applied", {"archive_key": "s3://b/k"}),
    }

    approve = rows[("agent.local_account.approve", None)]
    assert approve["actor_type"] == "admin"
    assert approve["actor_label"] == "juncheng@example.edu"
    assert (approve["target_type"], approve["target_id"]) == ("account", alice)
    assert approve["subject_label"] == approve["target_label"] == "alice@example.edu"
    assert approve["outcome"] == "applied"

    signup = rows[("agent.local_account.signup", None)]
    assert (signup["actor_type"], signup["actor_label"]) == ("anonymous", None)

    drain = rows[("agent.runner_host.drain", None)]
    assert (drain["actor_type"], drain["target_type"], drain["target_id"]) == (
        "break_glass",
        "host",
        "rtx",
    )
    assert rows[("agent.runner_host.set_active", None)]["target_id"] == "rtx2"
    assert rows[("agent.profiling.start", None)]["outcome"] == "refused"

    add = rows[("agent.compute.server.add", None)]
    assert (add["target_type"], add["outcome"], add["correlation_id"]) == (
        "compute_operation",
        "requested",
        "op-1",
    )
    retired = rows[("agent.something.retired", None)]
    assert (retired["actor_type"], retired["target_type"]) == ("service", "unknown")

    delete = rows[("agent.sandbox.delete.requested", "r1")]
    assert (delete["target_type"], delete["target_id"], delete["outcome"]) == (
        "sandbox",
        "s1",
        "requested",
    )
    assert delete["subject_label"] == "alice@example.edu"


async def test_rows_the_previous_release_writes_during_the_rollout_read_as_this_ones(
    store,
) -> None:
    """The previous release keeps running beside this one until the rollout ends.

    It writes only the columns it knows, its deletion worker writes a row per
    step, and its erasure deletes the account's rows. Each must still work and
    read as this release would have written it.
    """
    admin = await make_account(store, "juncheng@example.edu", admission="approved", admin=True)
    alice = await make_account(store, "alice@example.edu", admission="approved")
    config = _db_config()
    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DELETE FROM agent_admin_audit_log")
        await _legacy(conn, "agent.local_account.approve", f"admin:{admin}", alice, {"version": 2})
        await _legacy(
            conn, "agent.compute.server.add", f"admin:{admin}", None, {"operation_id": "op-roll"}
        )
        await conn.execute(
            "INSERT INTO agent_compute_operations (id, request_id, fingerprint, kind, state, "
            "completed_at) VALUES ('op-roll', 'req-roll', 'f', 'server.add', 'succeeded', NOW())"
        )
        await _legacy(
            conn, "agent.sandbox.delete.requested", f"admin:{admin}", alice, _deletion("s6", "r6")
        )
        await _legacy(conn, "agent.sandbox.delete.archived", _WORKER, alice, _deletion("s6", "r6"))
        await _legacy(
            conn,
            "agent.sandbox.delete.completed",
            _WORKER,
            alice,
            _deletion("s6", "r6") | {"archive_key": "s3://b/k6"},
        )
        approve = await conn.fetchrow(
            "SELECT * FROM agent_admin_audit_log WHERE action = 'agent.local_account.approve'"
        )
        results = await _results(conn)
    finally:
        await conn.close()

    assert (approve["target_type"], approve["target_id"], approve["outcome"]) == (
        "account",
        alice,
        "applied",
    )
    assert approve["actor_label"] == "juncheng@example.edu"
    assert approve["subject_label"] == approve["target_label"] == "alice@example.edu"
    assert results == {("requested", "r6"): ("applied", {"archive_key": "s3://b/k6"})}

    # What the page filters on and joins against is stored, not guessed.
    by_target = await store.audit_log.list_admin_actions(target=("account", alice), limit=10)
    assert [row["action"] for row in by_target] == ["agent.local_account.approve"]
    deletion = await store.audit_log.list_admin_actions(target=("sandbox", "s6"), limit=10)
    assert [(row["action"], row["result"]) for row in deletion] == [
        ("agent.sandbox.delete.requested", "applied")
    ]
    add = await store.audit_log.list_admin_actions(actions=["agent.compute.server.add"], limit=10)
    assert [(row["outcome"], row["result"]) for row in add] == [("requested", "applied")]

    # The previous release's erasure deletes the account's rows, results and all.
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DELETE FROM agent_admin_audit_log WHERE target_user_id = $1", alice)
        assert await conn.fetchval("SELECT count(*) FROM agent_admin_audit_outcomes") == 0
        await conn.execute("DELETE FROM agent_compute_operations WHERE id = 'op-roll'")
    finally:
        await conn.close()


async def test_a_row_this_release_writes_is_left_as_written(store) -> None:
    """The rollout trigger fills only rows that arrive without attribution."""
    config = _db_config()
    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DELETE FROM agent_admin_audit_log")
        await conn.execute(
            "INSERT INTO agent_admin_audit_log (action, actor_id, admin_ip, details, success, "
            "target_type, target_id, outcome) VALUES ('agent.runner_host.drain', 'admin-token', "
            "'203.0.113.9', '{\"host\": \"rtx\"}'::jsonb, TRUE, 'host', 'other', 'applied')"
        )
        row = await conn.fetchrow("SELECT target_id, actor_label FROM agent_admin_audit_log")
    finally:
        await conn.close()
    assert (row["target_id"], row["actor_label"]) == ("other", None)


async def test_the_downgrade_drops_the_attribution_and_keeps_the_rows(store) -> None:
    config = _db_config()
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DELETE FROM agent_admin_audit_outcomes")
        await conn.execute("DELETE FROM agent_admin_audit_log")
        await _legacy(conn, "agent.runner_host.drain", "admin-token", None, {"host": "rtx"})
    finally:
        await conn.close()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'agent_admin_audit_log'"
            )
        }
        assert not {"actor_type", "target_type", "outcome", "correlation_id"} & columns
        assert await conn.fetchval("SELECT to_regclass('agent_admin_audit_outcomes')") is None
        assert await conn.fetchval("SELECT count(*) FROM agent_admin_audit_log") == 1
    finally:
        await conn.close()
    apply_migrations(config)
