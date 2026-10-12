"""The rotation schema arrives empty and leaves without touching compute inventory."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0032_push_usage_and_workers"
_THIS = "0033_compute_node_rotation"


async def _tables(conn) -> set[str]:
    rows = await conn.fetch("SELECT table_name FROM information_schema.tables")
    return {row["table_name"] for row in rows}


async def _allocation_columns(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'agent_compute_allocations'"
    )
    return {row["column_name"] for row in rows}


async def _hold_check(conn) -> str:
    return await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'agent_host_admission_holds_purpose_check'"
    )


async def test_rotation_schema_round_trips(store):
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "agent_compute_node_registrations" not in await _tables(conn)
        assert "slot_host" not in await _allocation_columns(conn)
        assert "rotation" not in await _hold_check(conn)
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agent_compute_node_registrations") == 0
        assert {"slot_host", "slot_namespace", "predecessor_id"} <= await _allocation_columns(conn)
        assert "rotation" in await _hold_check(conn)
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        tables = await _tables(conn)
        assert "agent_compute_node_registrations" not in tables
        assert "agent_compute_allocations" in tables
        assert not {"slot_host", "slot_namespace", "predecessor_id"} & await _allocation_columns(
            conn
        )
        assert "rotation" not in await _hold_check(conn)
    finally:
        await conn.close()
    apply_migrations(config)
