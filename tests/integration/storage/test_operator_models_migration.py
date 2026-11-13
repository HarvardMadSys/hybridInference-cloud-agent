"""``0118_operator_model_connections`` against a real database.

It only adds tables. What Postgres settles is its downgrade: it refuses while
a provided connection exists or anything names one, drops the tables once
nothing does, and an upgrade puts them back.
"""

from __future__ import annotations

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import REPO, apply_migrations, downgrade_to

pytestmark = pytest.mark.dbtest

_BEFORE = "0117_connection_model_limit"
_THIS = "0118_operator_model_connections"
_ID = "oconn_" + "9" * 32
_TABLES = (
    "agent_operator_model_connections",
    "agent_operator_model_limits",
    "agent_operator_model_user_limits",
    "agent_operator_model_usage",
    "agent_operator_connection_residencies",
)


def test_the_revision_extends_the_one_migration_line() -> None:
    script = ScriptDirectory.from_config(Config(toml_file=str(REPO / "pyproject.toml")))
    assert script.get_revision(_THIS).down_revision == _BEFORE


async def _present(pool: asyncpg.Pool) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename = ANY($1::text[])", list(_TABLES)
        )
    return {row["tablename"] for row in rows}


@pytest.mark.asyncio
async def test_a_downgrade_waits_until_nothing_names_a_provided_connection(pool) -> None:
    config = _db_config()
    assert await _present(pool) == set(_TABLES)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_operator_model_connections
                (id, name, base_url, models, api_key_ciphertext)
            VALUES ($1, 'Provider', 'https://provider.example/v1', ARRAY['m1'], 'ciphertext')
            """,
            _ID,
        )
    try:
        with pytest.raises(Exception, match="provided model connections"):
            downgrade_to(config, _BEFORE)
        # Refused whole: nothing was dropped from under the connection.
        assert await _present(pool) == set(_TABLES)

        # A tombstone still counts: a turn may name it.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_operator_model_connections "
                "SET deleted_at = NOW(), api_key_ciphertext = NULL WHERE id = $1",
                _ID,
            )
        with pytest.raises(Exception, match="provided model connections"):
            downgrade_to(config, _BEFORE)

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_operator_model_connections WHERE id = $1", _ID)
        downgrade_to(config, _BEFORE)
        assert await _present(pool) == set()
    finally:
        apply_migrations(config, "head")
    assert await _present(pool) == set(_TABLES)


@pytest.mark.asyncio
async def test_the_schema_holds_what_no_flow_may_write(pool) -> None:
    insert = """
        INSERT INTO agent_operator_model_connections
            (id, name, base_url, models, api_key_ciphertext, enabled, is_default, deleted_at)
        VALUES ($1, $2, 'https://provider.example/v1', ARRAY['m1'], $3, $4, $5, $6)
    """
    async with pool.acquire() as conn:
        # An owner connection's id, a live connection without a key, an enabled tombstone.
        for values in (
            ("mconn_" + "9" * 32, "A", "ciphertext", False, False, None),
            (_ID, "A", None, False, False, None),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, *values)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert.replace("$6", "NOW()"), _ID, "A", None, True, False)
        # Two defaults, and two live connections of one name.
        async with conn.transaction():
            await conn.execute(insert, _ID, "Provider", "ciphertext", False, True, None)
            with pytest.raises(asyncpg.UniqueViolationError):
                async with conn.transaction():
                    await conn.execute(
                        insert, "oconn_" + "8" * 32, "Other", "ciphertext", False, True, None
                    )
            with pytest.raises(asyncpg.UniqueViolationError):
                async with conn.transaction():
                    await conn.execute(
                        insert, "oconn_" + "8" * 32, "PROVIDER", "ciphertext", False, False, None
                    )
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO agent_operator_model_limits "
                        "(operator_connection_id, model, user_daily_requests) VALUES ($1, 'm1', -1)",
                        _ID,
                    )
            await conn.execute("DELETE FROM agent_operator_model_connections WHERE id = $1", _ID)
