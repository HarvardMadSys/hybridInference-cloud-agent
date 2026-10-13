"""Blank native-session locators are repaired and rejected durably."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _reserve_retained_chat,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_PREVIOUS = "0061_mcp_catalog_sync_fence"


async def test_migration_repairs_blank_locator_state_and_enforces_invariant(
    pool,
    store,
) -> None:
    config = _db_config()
    # The current repository reads columns introduced after this migration.
    # Seed at head, then downgrade before injecting the historical bad locator.
    job, _, _ = await _reserve_retained_chat(store)
    downgrade_to(config, _PREVIOUS)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET native_session_id = E' \t\r\n ', resident_state = 'restarting'
                WHERE thread_id = $1
                """,
                job["thread_id"],
            )

        apply_migrations(config)

        async with pool.acquire() as conn:
            repaired = await conn.fetchrow(
                """
                SELECT native_session_id, resident_state
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                """,
                job["thread_id"],
            )
            constraint = await conn.fetchrow(
                """
                SELECT convalidated
                FROM pg_constraint
                WHERE conname = 'ck_agent_memberships_native_session_locator'
                """
            )
            assert repaired is not None
            assert dict(repaired) == {
                "native_session_id": None,
                "resident_state": "stopped",
            }
            assert constraint is not None and constraint["convalidated"] is True
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_session_id = E'\t\n'
                    WHERE thread_id = $1
                    """,
                    job["thread_id"],
                )

        downgrade_to(config, _PREVIOUS)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET native_session_id = E'\t\n', native_state = 'lost',
                    context_status = 'lost', resident_state = 'restarting'
                WHERE thread_id = $1
                """,
                job["thread_id"],
            )

        apply_migrations(config)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                    SELECT resident_state
                    FROM agent_thread_sandbox_memberships
                    WHERE thread_id = $1
                    """,
                    job["thread_id"],
                )
                == "lost"
            )

        downgrade_to(config, _PREVIOUS)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET native_session_id = E'\t\n'
                WHERE thread_id = $1
                """,
                job["thread_id"],
            )
            assert (
                await conn.fetchval(
                    """
                    SELECT native_session_id
                    FROM agent_thread_sandbox_memberships
                    WHERE thread_id = $1
                    """,
                    job["thread_id"],
                )
                == "\t\n"
            )
    finally:
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
