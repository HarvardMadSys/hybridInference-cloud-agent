"""Real PostgreSQL binding, adoption and concurrent-startup boundaries."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from urllib.parse import quote, urlencode

import asyncpg
import pytest
import pytest_asyncio

from cloud_agent.identity_namespace import IdentityNamespaceError, ensure_gateway_namespace
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]
ISSUER = "https://gateway.example"


@pytest.fixture(scope="module")
def db_config():
    name = os.environ.get("TEST_DB_NAME", "hybridinference_test_db")
    if "_test_" not in name:
        pytest.fail("Identity tests require an explicitly named test database.")
    config = {
        "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    apply_migrations(config)
    return config


@pytest_asyncio.fixture
async def pool(db_config):
    schema = "identity_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(**db_config)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(
        f'CREATE TABLE "{schema}".agent_identity_namespace '
        "(LIKE public.agent_identity_namespace INCLUDING ALL)"
    )
    # The runtime guard discovers ownership columns rather than guessing which
    # existing kinds of tasks or connections happen to have data today.
    await admin.execute(f'CREATE TABLE "{schema}".agent_threads (id TEXT, user_id TEXT)')
    local_pool = await asyncpg.create_pool(
        **db_config, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        yield local_pool
    finally:
        await local_pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_empty_database_records_issuer_and_restarts_idempotently(pool):
    assert await ensure_gateway_namespace(pool, ISSUER) == ISSUER
    assert await ensure_gateway_namespace(pool, ISSUER) == ISSUER
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_identity_namespace") == 1


@pytest.mark.parametrize("other", [None, "https://other.example", ISSUER + "/"])
async def test_different_or_missing_issuer_never_reuses_the_database(pool, other):
    await ensure_gateway_namespace(pool, ISSUER)
    with pytest.raises(IdentityNamespaceError, match="does not match"):
        await ensure_gateway_namespace(pool, other, adopt_existing=True)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace") == ISSUER


async def test_existing_owner_data_requires_explicit_adoption_and_is_preserved(pool):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO agent_threads VALUES ('thread-original', 'same-user-id')")
    with pytest.raises(IdentityNamespaceError, match="existing owner data"):
        await ensure_gateway_namespace(pool, ISSUER)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_identity_namespace") == 0
    await ensure_gateway_namespace(pool, ISSUER, adopt_existing=True)
    async with pool.acquire() as conn:
        assert tuple(await conn.fetchrow("SELECT id, user_id FROM agent_threads")) == (
            "thread-original",
            "same-user-id",
        )
    with pytest.raises(IdentityNamespaceError):
        await ensure_gateway_namespace(pool, "https://other.example", adopt_existing=True)


async def test_connection_only_history_also_requires_adoption(pool):
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE agent_future_connections (user_id TEXT)")
        await conn.execute("INSERT INTO agent_future_connections VALUES ('connection-owner')")
    with pytest.raises(IdentityNamespaceError, match="existing owner data"):
        await ensure_gateway_namespace(pool, ISSUER)


async def test_concurrent_different_issuers_cannot_both_bind(pool):
    answers = await asyncio.gather(
        ensure_gateway_namespace(pool, ISSUER),
        ensure_gateway_namespace(pool, "https://other.example"),
        return_exceptions=True,
    )
    assert sum(isinstance(answer, IdentityNamespaceError) for answer in answers) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_identity_namespace") == 1


async def test_unconfigured_empty_deployment_does_not_guess_a_namespace(pool):
    assert await ensure_gateway_namespace(pool, None) is None
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_identity_namespace") == 0


@pytest.fixture
def identity_cli(db_config):
    async def run(pool, *args, issuer=ISSUER):
        async with pool.acquire() as conn:
            schema = await conn.fetchval("SELECT current_schema()")
        user = quote(db_config["user"], safe="")
        password = quote(db_config["password"], safe="")
        dsn = (
            f"postgresql://{user}:{password}@{db_config['host']}:{db_config['port']}/"
            f"{db_config['database']}?{urlencode({'search_path': schema})}"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agent.identity_namespace",
            *args,
            env={**os.environ, "AGENT_DATABASE_URL": dsn, "GATEWAY_ISSUER": issuer},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, (stdout + stderr).decode()

    return run


async def test_deployment_check_refuses_legacy_data_until_explicit_adoption(pool, identity_cli):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO agent_threads VALUES ('original-thread', 'original-owner')")
    status, output = await identity_cli(pool, "--check")
    assert status != 0 and "existing owner data" in output
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_identity_namespace") == 0
    status, output = await identity_cli(pool, "--adopt-existing", "--issuer", ISSUER)
    assert status == 0, output
    status, output = await identity_cli(pool, "--check")
    assert status == 0, output
    async with pool.acquire() as conn:
        assert tuple(await conn.fetchrow("SELECT id, user_id FROM agent_threads")) == (
            "original-thread",
            "original-owner",
        )
    status, output = await identity_cli(pool, "--check", issuer="https://other.example")
    assert status != 0 and "does not match" in output


async def test_deployment_check_initializes_an_empty_database(pool, identity_cli):
    status, output = await identity_cli(pool, "--check")
    assert status == 0, output
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace") == ISSUER


@pytest.mark.parametrize(
    "args, issuer",
    [
        (["--adopt-existing"], ISSUER),
        (["--adopt-existing", "--issuer", "https://other.example"], ISSUER),
        (["--check", "--adopt-existing", "--issuer", ISSUER], ISSUER),
        (["--check", "--issuer", ISSUER], ISSUER),
        (["--check"], ""),
    ],
)
async def test_a_cli_check_cannot_silently_authorize_adoption(pool, identity_cli, args, issuer):
    status, _ = await identity_cli(pool, *args, issuer=issuer)
    assert status != 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_identity_namespace") == 0
