"""The V2 drain epoch upgrades and downgrades the published V1 fence safely."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0050_user_memory_edits"
_THIS = "0051_host_drain_fence_epoch"


async def _columns(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_runner_hosts'"
    )
    return {row["column_name"] for row in rows}


async def test_epoch_upgrade_backfills_guards_and_restores_exact_v1(store):
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "drain_fence_epoch" not in await _columns(conn)
        await conn.executemany(
            "INSERT INTO agent_runner_hosts (host, status, credential_hash) "
            "VALUES ($1, 'online', $2)",
            [("runner-a", "cred-a"), ("runner-b", "cred-b")],
        )
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        rows = await conn.fetch(
            "SELECT host, drain_fence_epoch FROM agent_runner_hosts ORDER BY host"
        )
        assert all(row["drain_fence_epoch"] is not None for row in rows)
        assert rows[0]["drain_fence_epoch"] != rows[1]["drain_fence_epoch"]

        initial_epoch = rows[0]["drain_fence_epoch"]
        chosen_epoch = uuid4()
        preserved_epoch = await conn.fetchval(
            "UPDATE agent_runner_hosts SET drain_fence_epoch=$2::uuid "
            "WHERE host=$1 RETURNING drain_fence_epoch",
            "runner-a",
            chosen_epoch,
        )
        assert preserved_epoch == initial_epoch

        credential_epoch = await conn.fetchval(
            "UPDATE agent_runner_hosts "
            "SET credential_hash='replacement', drain_fence_epoch=$2::uuid "
            "WHERE host=$1 RETURNING drain_fence_epoch",
            "runner-a",
            chosen_epoch,
        )
        assert credential_epoch not in (initial_epoch, chosen_epoch)

        receipt_token = uuid4()
        receipt_epoch = await conn.fetchval(
            """
            UPDATE agent_runner_hosts
            SET drain_fence_token=$2::uuid,
                drain_fence_status_before='online',
                drain_fence_active=FALSE
            WHERE host=$1
            RETURNING drain_fence_epoch
            """,
            "runner-a",
            receipt_token,
        )
        offline_epoch = await conn.fetchval(
            "UPDATE agent_runner_hosts SET status='offline' WHERE host=$1 "
            "RETURNING drain_fence_epoch",
            "runner-a",
        )
        assert offline_epoch != receipt_epoch

        active_token = uuid4()
        await conn.execute(
            """
            UPDATE agent_runner_hosts
            SET status='draining',
                drain_fence_token=$2::uuid,
                drain_fence_status_before='online',
                drain_fence_active=TRUE
            WHERE host=$1
            """,
            "runner-b",
            active_token,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("DELETE FROM agent_runner_hosts WHERE host=$1", "runner-b")
        await conn.execute(
            "UPDATE agent_runner_hosts SET status='online', drain_fence_active=FALSE WHERE host=$1",
            "runner-b",
        )

        await conn.execute(
            "INSERT INTO agent_runner_hosts (host, status) VALUES ('runner-c', 'online')"
        )
        default_epoch = await conn.fetchval(
            "SELECT drain_fence_epoch FROM agent_runner_hosts WHERE host='runner-c'"
        )
        assert default_epoch is not None
        assert default_epoch not in {row["drain_fence_epoch"] for row in rows}

        function_sql = await conn.fetchval(
            "SELECT pg_get_functiondef('agent_rotate_host_drain_fence_epoch()'::regprocedure)"
        )
        assert "compute_node_id IS DISTINCT FROM OLD.compute_node_id" in function_sql
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "drain_fence_epoch" not in await _columns(conn)
        receipt = await conn.fetchrow(
            "SELECT status, drain_fence_token, drain_fence_status_before, "
            "drain_fence_active FROM agent_runner_hosts WHERE host='runner-a'"
        )
        assert dict(receipt) == {
            "status": "offline",
            "drain_fence_token": None,
            "drain_fence_status_before": None,
            "drain_fence_active": False,
        }
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                UPDATE agent_runner_hosts
                SET drain_fence_token=$2::uuid,
                    drain_fence_status_before='online',
                    drain_fence_active=FALSE
                WHERE host=$1
                """,
                "runner-a",
                uuid4(),
            )
        assert (
            await conn.fetchval("SELECT to_regprocedure('agent_rotate_host_drain_fence_epoch()')")
            is None
        )
        assert (
            await conn.fetchval(
                "SELECT to_regprocedure('agent_guard_active_host_drain_fence_delete()')"
            )
            is None
        )
    finally:
        await conn.close()
    apply_migrations(config)
