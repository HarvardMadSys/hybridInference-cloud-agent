"""Postgres implementation of the Source-control authorization port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresSourceControlRepository:
    """Repository authorization and the GitLab credentials behind it."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def record_repo_grant(
        self, *, user_id: str, installation_id: int, account_login: str | None = None
    ) -> None:
        """Record that this user may use this installation."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_repo_grants (user_id, installation_id, account_login)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, installation_id)
                DO UPDATE SET account_login = EXCLUDED.account_login
                """,
                user_id,
                installation_id,
                account_login,
            )

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return the installations this user has connected."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT installation_id, account_login FROM agent_repo_grants "
                "WHERE user_id = $1 ORDER BY id",
                user_id,
            )
        return [
            {"installation_id": row["installation_id"], "account_login": row["account_login"]}
            for row in rows
        ]

    async def revoke_repo_grant(self, *, user_id: str, installation_id: int) -> bool:
        """Drop one connection. Uninstalling on GitHub is the other half."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_repo_grants WHERE user_id = $1 AND installation_id = $2 "
                "RETURNING id",
                user_id,
                installation_id,
            )
        return deleted is not None

    async def create_oauth_state(
        self,
        *,
        state_hash: str,
        user_id: str,
        provider: str,
        code_verifier_ciphertext: str | None,
        expires_at: Any,
    ) -> None:
        """Persist a short-lived state without retaining the bearer value."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM agent_oauth_states WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            await conn.execute(
                """
                INSERT INTO agent_oauth_states
                    (state_hash, user_id, provider, code_verifier_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                state_hash,
                user_id,
                provider,
                code_verifier_ciphertext,
                expires_at,
            )

    async def consume_oauth_state(
        self, *, state_hash: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Consume one live state exactly once for its owning user/provider."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_oauth_states
                SET consumed_at = NOW()
                WHERE state_hash = $1 AND user_id = $2 AND provider = $3
                  AND consumed_at IS NULL AND expires_at > NOW()
                RETURNING code_verifier_ciphertext
                """,
                state_hash,
                user_id,
                provider,
            )
        return dict(row) if row else None

    async def upsert_gitlab_connection(
        self,
        *,
        user_id: str,
        external_user_id: int,
        username: str,
        display_name: str | None,
        web_url: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> int:
        """Store one GitLab.com identity per platform user."""
        async with self._pool.acquire() as conn:
            connection_id = await conn.fetchval(
                """
                INSERT INTO agent_gitlab_connections
                    (user_id, external_user_id, username, display_name, web_url,
                     access_token_ciphertext, refresh_token_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (user_id) DO UPDATE SET
                    external_user_id = EXCLUDED.external_user_id,
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    web_url = EXCLUDED.web_url,
                    access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                    refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING id
                """,
                user_id,
                external_user_id,
                username,
                display_name,
                web_url,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return int(connection_id)

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the owning user's connection, including encrypted tokens."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, external_user_id, username, display_name, web_url,
                       access_token_ciphertext, refresh_token_ciphertext, expires_at
                FROM agent_gitlab_connections WHERE user_id = $1
                """,
                user_id,
            )
        return dict(row) if row else None

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Rotate encrypted OAuth tokens without crossing user ownership."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_gitlab_connections SET
                    access_token_ciphertext = $3,
                    refresh_token_ciphertext = $4,
                    expires_at = $5,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2
                RETURNING id
                """,
                connection_id,
                user_id,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return updated is not None

    async def delete_gitlab_connection(self, *, user_id: str, connection_id: int) -> bool:
        """Delete only a connection owned by the authenticated user."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_gitlab_connections WHERE user_id = $1 AND id = $2 RETURNING id",
                user_id,
                connection_id,
            )
        return deleted is not None
