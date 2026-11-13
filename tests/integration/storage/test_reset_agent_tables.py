"""The reset ends a session its test left running, and says what that session was doing."""

import asyncio
import warnings
from datetime import timedelta

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.support.db import LeftoverSessionWarning, reset_agent_tables

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _wait_until_active(pool, pid: int) -> None:
    async with pool.acquire() as conn:
        for _ in range(500):
            state = await conn.fetchval("SELECT state FROM pg_stat_activity WHERE pid = $1", pid)
            if state == "active":
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"session {pid} never started its statement")


async def test_reset_ends_a_statement_that_outlived_its_test(pool):
    # The shape of a timed-out test: its statement still runs, and holds a
    # lock the TRUNCATE needs, so without the reset ending it the TRUNCATE
    # would wait out the whole sleep behind it.
    leftover = await pool.acquire()
    pid = leftover.get_server_pid()
    running = asyncio.create_task(
        leftover.execute("SELECT pg_sleep(60) FROM (SELECT count(*) FROM agent_jobs) AS held")
    )
    try:
        await _wait_until_active(pool, pid)
        await asyncio.sleep(0.2)
        with pytest.warns(LeftoverSessionWarning, match=rf"session {pid}, active .*pg_sleep\(60\)"):
            async with pool.acquire() as conn:
                await asyncio.wait_for(
                    reset_agent_tables(conn, leftover_after=timedelta(seconds=0.1)), timeout=15
                )
        with pytest.raises(
            (asyncpg.exceptions.AdminShutdownError, asyncpg.exceptions.ConnectionDoesNotExistError)
        ):
            await running
    finally:
        running.cancel()
        await pool.release(leftover)


async def test_reset_leaves_a_session_that_is_merely_busy(pool):
    busy = await pool.acquire()
    running = asyncio.create_task(busy.execute("SELECT pg_sleep(1)"))
    try:
        await _wait_until_active(pool, busy.get_server_pid())
        with warnings.catch_warnings():
            warnings.simplefilter("error", LeftoverSessionWarning)
            async with pool.acquire() as conn:
                await reset_agent_tables(conn)
        # Not terminated: it finishes its statement normally.
        assert await running == "SELECT 1"
    finally:
        running.cancel()
        await pool.release(busy)
