"""The registration table arrives empty and leaves without touching compute inventory."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0029_work_available_notify"
_THIS = "0030_compute_node_registration"


async def _tables(conn) -> set[str]:
    rows = await conn.fetch("SELECT table_name FROM information_schema.tables")
    return {row["table_name"] for row in rows}


async def test_registration_table_round_trips(store):
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert "agent_compute_node_registrations" not in await _tables(conn)
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agent_compute_node_registrations") == 0
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        tables = await _tables(conn)
        assert "agent_compute_node_registrations" not in tables
        assert "agent_compute_allocations" in tables
    finally:
        await conn.close()
    apply_migrations(config)
