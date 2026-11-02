"""Real PostgreSQL: a database is bound to Cloud Agent's accounts once, or refused.

An empty database is bound the first time it starts. One whose owners are
Gateway accounts -- it records a Gateway issuer, or holds owner data with no
binding, as every Gateway-era database does -- is refused with the cutover
instructions, and nothing in it changes. ``--check`` answers the same question
read-only, on any schema revision, including one from before the baseline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import asyncpg
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from scripts.convert_legacy_database_copy import fingerprints
from tests.support.migrations import apply_migrations

from cloud_agent.accounts.config import CUTOVER_RUNBOOK
from cloud_agent.identity_namespace import (
    GATEWAY_BOUND,
    UNBOUND_OWNER_DATA,
    IdentityNamespaceError,
    check_namespace,
    ensure_local_namespace,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]
ISSUER = "https://gateway.example"
LEGACY_SCHEMA = Path(__file__).resolve().parents[2] / "fixtures/legacy_schema/0025.sql"


def _options() -> dict:
    return {
        "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest.fixture(scope="module")
def db_config():
    name = os.environ.get("TEST_DB_NAME", "hybridinference_test_db")
    if "_test_" not in name:
        pytest.fail("Identity tests require an explicitly named test database.")
    config = {**_options(), "database": name}
    apply_migrations(config)
    return config


@pytest_asyncio.fixture
async def pool(db_config):
    """A schema of its own holding the tables the namespace guard reads."""
    schema = "identity_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(**db_config)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    for name in ("agent_identity_namespace", "agent_local_identity_namespace"):
        await admin.execute(f'CREATE TABLE "{schema}".{name} (LIKE public.{name} INCLUDING ALL)')
    # The guard discovers ownership columns rather than guessing which kinds
    # of tasks or connections happen to have data today.
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


async def _bindings(pool) -> tuple[int, int]:
    async with pool.acquire() as conn:
        return (
            await conn.fetchval("SELECT count(*) FROM agent_identity_namespace"),
            await conn.fetchval("SELECT count(*) FROM agent_local_identity_namespace"),
        )


async def _bind_to_gateway(pool) -> None:
    """What a Gateway-era deployment recorded when it first started."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_identity_namespace (gateway_issuer) VALUES ($1)", ISSUER
        )


async def test_an_empty_database_is_bound_once_and_restarts_idempotently(pool):
    async with pool.acquire() as conn:
        assert await check_namespace(conn) is False
    await ensure_local_namespace(pool)
    await ensure_local_namespace(pool)
    assert await _bindings(pool) == (0, 1)
    async with pool.acquire() as conn:
        assert await check_namespace(conn) is True
        # Data written after binding is the accounts' own.
        await conn.execute("INSERT INTO agent_threads VALUES ('t', 'account-id')")
    await ensure_local_namespace(pool)


async def test_a_gateway_bound_database_is_refused_with_the_cutover_and_kept(pool):
    await _bind_to_gateway(pool)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO agent_threads VALUES ('t', 'gateway-owner')")
    with pytest.raises(IdentityNamespaceError) as refused:
        await ensure_local_namespace(pool)
    assert str(refused.value) == GATEWAY_BOUND
    assert CUTOVER_RUNBOOK in GATEWAY_BOUND
    assert await _bindings(pool) == (1, 0)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace") == ISSUER
        assert tuple(await conn.fetchrow("SELECT * FROM agent_threads")) == ("t", "gateway-owner")


async def test_a_gateway_issuer_alone_is_refused_even_without_owner_data(pool):
    """An issuer names whose owners these were; no rows does not make them ours."""
    await _bind_to_gateway(pool)
    with pytest.raises(IdentityNamespaceError, match="owners are Gateway accounts"):
        await ensure_local_namespace(pool)
    assert await _bindings(pool) == (1, 0)


async def test_owner_data_without_a_binding_is_refused_and_preserved(pool):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO agent_threads VALUES ('t', 'gateway-owner')")
    with pytest.raises(IdentityNamespaceError) as refused:
        await ensure_local_namespace(pool)
    assert str(refused.value) == UNBOUND_OWNER_DATA
    assert await _bindings(pool) == (0, 0)
    async with pool.acquire() as conn:
        assert tuple(await conn.fetchrow("SELECT * FROM agent_threads")) == ("t", "gateway-owner")


async def test_owner_data_in_a_table_the_guard_was_not_told_about_is_refused(pool):
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE agent_future_connections (user_id TEXT)")
        await conn.execute("INSERT INTO agent_future_connections VALUES ('connection-owner')")
    with pytest.raises(IdentityNamespaceError, match="no account binding"):
        await ensure_local_namespace(pool)
    assert await _bindings(pool) == (0, 0)


async def test_concurrent_first_starts_bind_exactly_once(pool):
    await asyncio.gather(*(ensure_local_namespace(pool) for _ in range(4)))
    assert await _bindings(pool) == (0, 1)


@pytest.fixture
def namespace_cli():
    async def run(dsn: str, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cloud_agent.identity_namespace",
            *args,
            # A stale Gateway issuer in the environment decides nothing.
            env={**os.environ, "AGENT_DATABASE_URL": dsn, "GATEWAY_ISSUER": ISSUER},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, (stdout + stderr).decode()

    return run


def _dsn(database: str, *, schema: str | None = None) -> str:
    options = _options()
    user = quote(options["user"], safe="")
    password = quote(options["password"], safe="")
    query = f"?{urlencode({'search_path': schema})}" if schema else ""
    return f"postgresql://{user}:{password}@{options['host']}:{options['port']}/{database}{query}"


async def _schema_dsn(pool, db_config) -> str:
    async with pool.acquire() as conn:
        schema = await conn.fetchval("SELECT current_schema()")
    return _dsn(db_config["database"], schema=schema)


async def test_the_check_is_read_only_on_an_empty_database(pool, db_config, namespace_cli):
    status, output = await namespace_cli(await _schema_dsn(pool, db_config), "--check")
    assert status == 0, output
    assert "will be bound" in output and "nothing was changed" in output
    assert await _bindings(pool) == (0, 0), "a check never binds"


async def test_the_check_passes_a_bound_database(pool, db_config, namespace_cli):
    await ensure_local_namespace(pool)
    status, output = await namespace_cli(await _schema_dsn(pool, db_config), "--check")
    assert status == 0 and "is bound" in output, output


@pytest.mark.parametrize("gateway_issuer", [True, False])
async def test_the_check_refuses_a_gateway_database(pool, db_config, namespace_cli, gateway_issuer):
    if gateway_issuer:
        await _bind_to_gateway(pool)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO agent_threads VALUES ('t', 'gateway-owner')")
    status, output = await namespace_cli(await _schema_dsn(pool, db_config), "--check")
    assert status != 0
    assert (GATEWAY_BOUND if gateway_issuer else UNBOUND_OWNER_DATA) in output
    assert await _bindings(pool) == (int(gateway_issuer), 0)


async def test_the_cli_only_checks(pool, db_config, namespace_cli):
    """There is no switch that adopts, binds or maps a database."""
    dsn = await _schema_dsn(pool, db_config)
    for args in ((), ("--adopt-existing",), ("--check", "--adopt-existing"), ("--bind",)):
        status, _ = await namespace_cli(dsn, *args)
        assert status != 0, args
    assert await _bindings(pool) == (0, 0)


@pytest.fixture
def gateway_era_database():
    """A database at the pre-baseline Gateway-era revision, with owners' rows in it."""
    options = _options()
    name = "agent_legacy_namespace_" + uuid.uuid4().hex
    with psycopg.connect(dbname="postgres", autocommit=True, **options) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(dbname=name, **options) as conn:
                conn.execute(LEGACY_SCHEMA.read_text())
                conn.execute("SET search_path=public")
                conn.execute(
                    "INSERT INTO agent_threads(id,user_id,repo,title) "
                    "VALUES ('t','gateway-owner','repo','title')"
                )
                conn.execute(
                    "INSERT INTO agent_jobs(id,thread_id,user_id,turn_no,repo,task_prompt,"
                    "runtime,model,state) VALUES "
                    "('j','t','gateway-owner',1,'repo','prompt','codex','model','succeeded')"
                )
            yield name
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _whole_database(name: str) -> tuple[list, dict]:
    """Every table and column, and a hash of every row: what a write would change."""
    with psycopg.connect(dbname=name, **_options()) as conn:
        columns = conn.execute(
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY 1, 2, 3"
        ).fetchall()
        return columns, fingerprints(conn, "public")


async def test_the_check_refuses_a_gateway_era_schema_and_changes_nothing(
    gateway_era_database, namespace_cli
):
    """The cutover's first step, against a database from before the baseline.

    That schema has neither binding table. The check must still answer, and an
    operator who runs it on the database they are about to archive must find it
    exactly as it was.
    """
    before = _whole_database(gateway_era_database)
    assert before[1]["agent_threads"]["rows"] == 1
    status, output = await namespace_cli(_dsn(gateway_era_database), "--check")
    assert status != 0
    assert UNBOUND_OWNER_DATA in output and CUTOVER_RUNBOOK in output
    assert _whole_database(gateway_era_database) == before
