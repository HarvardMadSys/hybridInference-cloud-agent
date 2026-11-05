"""The GitHub account revision: a new table beside the grants, and nothing else."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0023_pending_messages"
_THIS = "0024_github_accounts"


async def _grants(conn) -> list[dict]:
    return [dict(row) for row in await conn.fetch("SELECT * FROM agent_repo_grants ORDER BY id")]


async def test_the_revision_adds_the_account_table_and_leaves_grants_as_they_were(store):
    config = _db_config()
    conn = await asyncpg.connect(**config)
    try:
        # A grant from before this revision: no account row to go with it.
        await conn.execute(
            "INSERT INTO agent_repo_grants (user_id, installation_id, account_login) "
            "VALUES ('user-owner', 7, 'acme')"
        )
    finally:
        await conn.close()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert await conn.fetchval("SELECT to_regclass('agent_github_accounts')") is None
        grants = await _grants(conn)
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        # A connection made before this revision is still exactly what it was:
        # grants, and no account, which the service reads as connected.
        assert grants and await _grants(conn) == grants
        assert await conn.fetchval("SELECT count(*) FROM agent_github_accounts") == 0
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO agent_github_accounts (user_id, github_user_id, login) "
                "VALUES ('user-owner', 1, '')"
            )
        await conn.execute(
            "INSERT INTO agent_github_accounts (user_id, github_user_id, login) "
            "VALUES ('user-owner', 1, 'octocat')"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO agent_github_accounts (user_id, github_user_id, login) "
                "VALUES ('user-owner', 2, 'hubot')"
            )
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert await conn.fetchval("SELECT to_regclass('agent_github_accounts')") is None
        assert await _grants(conn) == grants
    finally:
        await conn.close()
    apply_migrations(config)
