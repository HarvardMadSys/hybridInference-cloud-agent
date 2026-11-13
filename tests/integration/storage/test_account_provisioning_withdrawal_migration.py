"""``0119_drop_account_provisioning`` against a real database.

It drops the two provisioning tables and takes the four provisioning settings
out of the stored document, the secret among them, leaving every other
setting as it was. A downgrade puts the tables back empty.
"""

from __future__ import annotations

import json

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import REPO, apply_migrations, downgrade_to

pytestmark = pytest.mark.dbtest

_BEFORE = "0118_operator_model_connections"
_THIS = "0119_drop_account_provisioning"
_TABLES = ("agent_account_provisioning", "agent_account_provisioning_activation")
_WITHDRAWN = {
    "accounts.inference_provisioning_enabled": True,
    "accounts.inference_provisioning_api_url": "https://inference.example",
    "accounts.inference_provisioning_web_url": "https://inference.example",
    "accounts.inference_provisioning_dispatch_token": {"ciphertext": "sealed"},
}
_KEPT = {"log_level": "INFO"}


def test_the_revision_extends_the_one_migration_line() -> None:
    script = ScriptDirectory.from_config(Config(toml_file=str(REPO / "pyproject.toml")))
    assert script.get_revision(_THIS).down_revision == _BEFORE
    assert script.get_heads() == [_THIS]


async def _present(pool: asyncpg.Pool) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename = ANY($1::text[])", list(_TABLES)
        )
    return {row["tablename"] for row in rows}


async def _settings(pool: asyncpg.Pool) -> tuple[int, dict] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT revision, "values" FROM agent_settings WHERE id')
    return None if row is None else (int(row["revision"]), json.loads(row["values"]))


async def _store(pool: asyncpg.Pool, saved: tuple[int, dict] | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_settings")
        if saved is not None:
            await conn.execute(
                'INSERT INTO agent_settings (id, revision, "values") VALUES (TRUE, $1, $2::jsonb)',
                saved[0],
                json.dumps(saved[1]),
            )


@pytest.mark.asyncio
async def test_the_tables_and_the_stored_settings_go_and_nothing_else_does(pool) -> None:
    config = _db_config()
    assert await _present(pool) == set()
    before = await _settings(pool)
    try:
        downgrade_to(config, _BEFORE)
        assert await _present(pool) == set(_TABLES)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_account_provisioning (account_id, state) VALUES ('u1', 'invited')"
            )
        await _store(pool, (7, {**_KEPT, **_WITHDRAWN}))

        apply_migrations(config, "head")

        assert await _present(pool) == set()
        # A new revision, so a running process adopts the document without them.
        assert await _settings(pool) == (8, _KEPT)

        # A document that never held them is left exactly as it was.
        downgrade_to(config, _BEFORE)
        await _store(pool, (3, _KEPT))
        apply_migrations(config, "head")
        assert await _settings(pool) == (3, _KEPT)
    finally:
        apply_migrations(config, "head")
        await _store(pool, before)


@pytest.mark.asyncio
async def test_a_downgrade_puts_the_tables_back_empty(pool) -> None:
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        assert await _present(pool) == set(_TABLES)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM agent_account_provisioning") == 0
            # The shape the release that wrote them expects.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO agent_account_provisioning (account_id, state) "
                    "VALUES ('u1', 'unknown')"
                )
    finally:
        apply_migrations(config, "head")
    assert await _present(pool) == set()
