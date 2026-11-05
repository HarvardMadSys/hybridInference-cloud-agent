"""The control plane's pool hands a connection back without a reset query.

asyncpg's default reset costs a round trip on every release, and every
repository call acquires a connection of its own. Skipping it is safe only
because nothing leaves session state on a pooled connection, and these tests
pin both halves of that: a release runs no reset, so session state would
survive it (which is why none may be left), and a transaction left open is
still rolled back.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import pytest

from cloud_agent.bootstrap.lifespan import DATABASE_POOL_SIZE, create_database_pool

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _dsn() -> str:
    base = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = base if worker == "master" else f"{base}_{worker}"
    if "_test_" not in name:
        pytest.fail(f"SAFETY: TEST_DB_NAME='{name}' is not a test database.")
    user = quote(os.getenv("TEST_DB_USER", "postgres"), safe="")
    password = quote(os.getenv("TEST_DB_PASSWORD", "postgres"), safe="")
    host = os.getenv("TEST_DB_HOST", "localhost")
    port = os.getenv("TEST_DB_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


async def test_a_released_connection_is_not_reset_but_its_transaction_is_rolled_back():
    try:
        pool = await create_database_pool(_dsn())
    except OSError as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    try:
        assert pool.get_max_size() == DATABASE_POOL_SIZE
        async with pool.acquire() as conn:
            backend = await conn.fetchval("SELECT pg_backend_pid()")
            await conn.execute("SET application_name = 'left behind'")
        async with pool.acquire() as conn:
            # The pool hands back the connection released last.
            assert await conn.fetchval("SELECT pg_backend_pid()") == backend
            assert await conn.fetchval("SHOW application_name") == "left behind"
            assert await conn.fetchval("SHOW jit") == "off"

        conn = await pool.acquire()
        await conn.execute("BEGIN")
        assert await conn.fetchval("SELECT txid_current_if_assigned() IS NULL") is True
        await conn.fetchval("SELECT txid_current()")
        await pool.release(conn)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT pg_backend_pid()") == backend
            assert not conn.is_in_transaction()
            assert await conn.fetchval("SELECT txid_current_if_assigned()") is None
    finally:
        await pool.close()
