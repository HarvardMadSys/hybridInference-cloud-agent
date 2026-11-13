"""A host's claim meets its database restarting: told to retry, then served again.

Against the dbtest tier's real PostgreSQL, the real schema, the real routes and
the pool the service builds (``create_database_pool``). What a restart does to
a client is what a test can do cheaply on a shared server: end the sessions
with ``pg_terminate_backend``, which sends each one the same notice a server
shutting down sends, and closes it. On 2026-11-12 a host's claim that met this
answered 500.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.support.db import assert_pool_idle, reset_agent_tables
from tests.support.local_accounts import db_config
from tests.support.migrations import apply_migrations

from cloud_agent.api._database_unavailable import RETRY_AFTER_SECONDS
from cloud_agent.app import create_app
from cloud_agent.bootstrap.lifespan import DATABASE_POOL_SIZE, create_database_pool
from cloud_agent.storage.assembly import Storage
from cloud_agent.tokens import mint_host_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_CLAIM = {"worker_id": "worker-1", "lease_ttl_seconds": 60}


@pytest.fixture
def application() -> str:
    """Names this test's pool sessions in ``pg_stat_activity``: it ends and counts only those."""
    return f"database-restart-{secrets.token_hex(4)}"


@pytest_asyncio.fixture
async def pool(application: str) -> AsyncIterator[asyncpg.Pool]:
    config = db_config()
    apply_migrations(config)
    dsn = "postgresql://{user}:{password}@{host}:{port}/{database}".format(**config)
    dsn += f"?application_name={application}"
    pool = await create_database_pool(dsn)
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


@pytest_asyncio.fixture
async def admin() -> AsyncIterator[asyncpg.Connection]:
    """A session of its own, as an operator's would be, to watch and end the pool's."""
    conn = await asyncpg.connect(**db_config())
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def client(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, dict[str, str]]]:
    """The main listener over the pool, and a host credential it does not know."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "database-restart-test-secret")
    app = create_app()
    app.state.storage = Storage(pool)
    headers = {"Authorization": f"Bearer {mint_host_token(host='restart-host')}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, headers


async def _pool_backends(admin: asyncpg.Connection, application: str) -> set[int]:
    rows = await admin.fetch(
        "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
        "AND application_name = $1",
        application,
    )
    return {row["pid"] for row in rows}


async def _waiting_for_the_host_table(admin: asyncpg.Connection, application: str) -> int:
    async with asyncio.timeout(10):
        while True:
            pid = await admin.fetchval(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                "AND application_name = $1 AND wait_event_type = 'Lock' "
                "AND query LIKE '%agent_runner_hosts%'",
                application,
            )
            if pid is not None:
                return int(pid)
            await asyncio.sleep(0.01)


async def test_a_claim_whose_session_is_ended_is_told_to_retry_and_the_next_is_served(
    pool: asyncpg.Pool,
    admin: asyncpg.Connection,
    application: str,
    client: tuple[AsyncClient, dict[str, str]],
) -> None:
    http, headers = client
    # The database answers: it knows no such host.
    assert (
        await http.post("/v1/agent/worker/claim", json=_CLAIM, headers=headers)
    ).status_code == 401

    # Hold the claim's host lookup where its session can be ended mid-statement.
    response: Any
    locker = await asyncpg.connect(**db_config())
    try:
        async with locker.transaction():
            await locker.execute("LOCK TABLE agent_runner_hosts IN ACCESS EXCLUSIVE MODE")
            claim = asyncio.create_task(
                http.post("/v1/agent/worker/claim", json=_CLAIM, headers=headers)
            )
            waiting = await _waiting_for_the_host_table(admin, application)
            assert await admin.fetchval("SELECT pg_terminate_backend($1)", waiting)
            response = await claim
    finally:
        await locker.close()

    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert response.json()["detail"]["error"]["type"] == "database_unavailable"
    # The next claim is served on a live connection, and nothing was lost.
    assert (
        await http.post("/v1/agent/worker/claim", json=_CLAIM, headers=headers)
    ).status_code == 401
    await assert_pool_idle(pool)


async def test_after_every_pooled_session_is_ended_the_pool_reopens_them_and_serves(
    pool: asyncpg.Pool,
    admin: asyncpg.Connection,
    application: str,
    client: tuple[AsyncClient, dict[str, str]],
) -> None:
    http, headers = client
    before = await _pool_backends(admin, application)
    assert len(before) == DATABASE_POOL_SIZE

    await admin.execute(
        "SELECT pg_terminate_backend(pid) FROM unnest($1::int[]) AS pid", sorted(before)
    )

    # Reopened by the pool itself, before any request asks for one: the
    # ended sessions gone, and as many new ones in their place.
    async with asyncio.timeout(10):
        while True:
            after = await _pool_backends(admin, application)
            if not after & before and len(after) == DATABASE_POOL_SIZE:
                break
            await asyncio.sleep(0.02)
    assert (
        await http.post("/v1/agent/worker/claim", json=_CLAIM, headers=headers)
    ).status_code == 401
    await assert_pool_idle(pool)
