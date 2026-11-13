"""The control plane's pool comes through a database restart whole.

A PostgreSQL shutting down tells each idle client so, then closes its socket.
A statement that reaches a pooled connection between the two fails -- the
request is answered 503 (``tests/unit/test_database_unavailable.py``) -- and
asyncpg 0.31 then kept that connection's pool slot taken for good
(MagicStack/asyncpg#1324): each restart under load could leave the pool smaller,
until every repository call waited forever for a connection. Real PostgreSQL
cannot be held between the notice and the close, so
:mod:`tests.support.fake_postgres` does it, against the pool the service builds.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from tests.support.fake_postgres import ADMIN_SHUTDOWN, FakePostgres

from cloud_agent.api._database_unavailable import database_unavailable
from cloud_agent.bootstrap.lifespan import DATABASE_POOL_SIZE, create_database_pool


async def test_a_connection_the_server_ended_while_idle_gives_its_slot_back() -> None:
    async with FakePostgres() as server:
        pool = await create_database_pool(server.dsn)
        try:
            server.end_sessions_while_idle(*ADMIN_SHUTDOWN)
            await asyncio.sleep(0.05)  # the notice is read; the close has not come
            with pytest.raises(asyncpg.InternalClientError) as raced:
                async with pool.acquire() as conn:
                    await conn.execute("SELECT 1")
            assert database_unavailable(raced.value)
            server.close_sessions()
            await asyncio.sleep(0.1)  # the pool sees every close before it is asked again

            # Every slot can be taken again, each on a working connection.
            async with asyncio.timeout(3):
                held = [await pool.acquire() for _ in range(DATABASE_POOL_SIZE)]
            for conn in held:
                assert await conn.execute("SELECT 1") == "SELECT 1"
                await pool.release(conn)
        finally:
            pool.terminate()


async def test_the_pool_reopens_its_connections_after_a_restart_before_anyone_asks() -> None:
    """The first request after a restart finds a connection open, rather than opening one."""
    async with FakePostgres() as server:
        pool = await create_database_pool(server.dsn)
        try:
            assert server.connections == DATABASE_POOL_SIZE
            server.end_sessions_while_idle(*ADMIN_SHUTDOWN)
            server.close_sessions()

            # Nothing acquires a connection here, so each one the server accepts
            # from now on the pool opened by itself. The server counts it as
            # it accepts it, before asyncpg's handshake is through and the pool
            # counts it as well: the wait is on both, and only ends within the
            # time limit if the pool has every connection back.
            async with asyncio.timeout(3):
                while (
                    server.connections < 2 * DATABASE_POOL_SIZE
                    or pool.get_size() < DATABASE_POOL_SIZE
                ):
                    await asyncio.sleep(0.01)
            # One new connection for each that was lost.
            assert server.connections == 2 * DATABASE_POOL_SIZE
        finally:
            pool.terminate()
