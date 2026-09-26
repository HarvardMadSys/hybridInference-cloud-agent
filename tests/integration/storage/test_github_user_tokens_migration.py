"""The user-token revision: columns added to the account row, nothing else touched."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0024_github_accounts"
_THIS = "0025_github_user_tokens"


async def _rows(conn, table: str, order: str) -> list[dict]:
    return [dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY {order}")]


async def test_an_existing_connection_comes_through_with_no_token(store):
    """Connections from before this revision have to be made again — and nothing else."""
    await store.source_control.connect_github_account(
        user_id="user-owner",
        github_user_id=583231,
        login="octocat",
        installations=[{"installation_id": 7, "account_login": "acme"}],
    )
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        accounts = await _rows(conn, "agent_github_accounts", "user_id")
        grants = await _rows(conn, "agent_repo_grants", "id")
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        assert await _rows(conn, "agent_github_accounts", "user_id") == [
            {
                **row,
                "access_token_ciphertext": None,
                "access_expires_at": None,
                "refresh_token_ciphertext": None,
                "refresh_expires_at": None,
                "refresh_generation": 0,
                "refresh_lease_expires_at": None,
            }
            for row in accounts
        ]
        assert grants and await _rows(conn, "agent_repo_grants", "id") == grants
        # A refresh token with no access token is not a state anything writes.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_github_accounts SET refresh_token_ciphertext = 'ct' "
                "WHERE user_id = 'user-owner'"
            )
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert await _rows(conn, "agent_github_accounts", "user_id") == accounts
        assert await _rows(conn, "agent_repo_grants", "id") == grants
    finally:
        await conn.close()
    apply_migrations(config)
