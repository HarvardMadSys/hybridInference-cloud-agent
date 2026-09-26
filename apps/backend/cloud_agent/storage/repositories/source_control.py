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

    async def connect_github_account(
        self,
        *,
        user_id: str,
        github_user_id: int,
        login: str,
        installations: list[dict[str, Any]],
        access_token_ciphertext: str | None = None,
        access_expires_at: Any = None,
        refresh_token_ciphertext: str | None = None,
        refresh_expires_at: Any = None,
    ) -> None:
        """Record the GitHub account and make its grants exactly ``installations``."""
        # One transaction, because the two are one answer: GitHub said who this
        # is and what they can reach *now*. Grants were only ever added, so an
        # installation the user had lost — removed from the organization, the
        # App uninstalled — stayed granted until someone disconnected it by
        # hand. Replacing them is what makes a reconnect mean what GitHub says
        # today, including when the user reconnects as another GitHub account.
        installation_ids = [int(item["installation_id"]) for item in installations]
        async with self._pool.acquire() as conn, conn.transaction():
            # A new token pair replaces the old outright, and any refresh in
            # flight for the old one loses its lease: the generation moves.
            await conn.execute(
                """
                INSERT INTO agent_github_accounts
                    (user_id, github_user_id, login, access_token_ciphertext,
                     access_expires_at, refresh_token_ciphertext, refresh_expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (user_id) DO UPDATE SET
                    github_user_id = EXCLUDED.github_user_id,
                    login = EXCLUDED.login,
                    access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                    access_expires_at = EXCLUDED.access_expires_at,
                    refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                    refresh_expires_at = EXCLUDED.refresh_expires_at,
                    refresh_generation = agent_github_accounts.refresh_generation + 1,
                    refresh_lease_expires_at = NULL,
                    updated_at = NOW()
                """,
                user_id,
                github_user_id,
                login,
                access_token_ciphertext,
                access_expires_at,
                refresh_token_ciphertext,
                refresh_expires_at,
            )
            await conn.execute(
                "DELETE FROM agent_repo_grants "
                "WHERE user_id = $1 AND NOT (installation_id = ANY($2::bigint[]))",
                user_id,
                installation_ids,
            )
            for item in installations:
                await conn.execute(
                    """
                    INSERT INTO agent_repo_grants (user_id, installation_id, account_login)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, installation_id)
                    DO UPDATE SET account_login = EXCLUDED.account_login
                    """,
                    user_id,
                    int(item["installation_id"]),
                    item.get("account_login"),
                )

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the GitHub account this user connected, if any."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT github_user_id, login, access_token_ciphertext, access_expires_at,
                       refresh_token_ciphertext, refresh_expires_at, refresh_generation,
                       refresh_lease_expires_at, connected_at, updated_at
                FROM agent_github_accounts WHERE user_id = $1
                """,
                user_id,
            )
        return dict(row) if row else None

    async def claim_github_token_refresh(
        self, *, user_id: str, lease_seconds: int, margin_seconds: int
    ) -> dict[str, Any] | None:
        """Take the lease to refresh an expiring token, or ``None`` if it is not ours."""
        # Only a token that still needs it, and only while nobody else holds a
        # live lease: a caller that lost the race reads the row again and finds
        # either the new token or one still valid for a few more minutes.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_github_accounts SET
                    refresh_generation = refresh_generation + 1,
                    refresh_lease_expires_at = NOW() + make_interval(secs => $2)
                WHERE user_id = $1
                  AND refresh_token_ciphertext IS NOT NULL
                  AND access_expires_at IS NOT NULL
                  AND access_expires_at <= NOW() + make_interval(secs => $3)
                  AND (refresh_lease_expires_at IS NULL OR refresh_lease_expires_at <= NOW())
                RETURNING refresh_generation, refresh_token_ciphertext
                """,
                user_id,
                lease_seconds,
                margin_seconds,
            )
        return dict(row) if row else None

    async def store_github_tokens(
        self,
        *,
        user_id: str,
        refresh_generation: int,
        access_token_ciphertext: str,
        access_expires_at: Any,
        refresh_token_ciphertext: str | None,
        refresh_expires_at: Any,
    ) -> bool:
        """Write a refreshed token pair while the lease is still ours, and release it."""
        async with self._pool.acquire() as conn:
            written = await conn.fetchval(
                """
                UPDATE agent_github_accounts SET
                    access_token_ciphertext = $3,
                    access_expires_at = $4,
                    refresh_token_ciphertext = $5,
                    refresh_expires_at = $6,
                    refresh_lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE user_id = $1 AND refresh_generation = $2
                RETURNING user_id
                """,
                user_id,
                refresh_generation,
                access_token_ciphertext,
                access_expires_at,
                refresh_token_ciphertext,
                refresh_expires_at,
            )
        return written is not None

    async def clear_github_tokens(self, *, user_id: str, access_token_ciphertext: str) -> bool:
        """Forget a token GitHub no longer honours, unless it was replaced meanwhile."""
        # Matched on the ciphertext that failed: a reconnect or a refresh that
        # landed in between wrote a different one, which must survive this.
        async with self._pool.acquire() as conn:
            cleared = await conn.fetchval(
                """
                UPDATE agent_github_accounts SET
                    access_token_ciphertext = NULL,
                    access_expires_at = NULL,
                    refresh_token_ciphertext = NULL,
                    refresh_expires_at = NULL,
                    refresh_generation = refresh_generation + 1,
                    refresh_lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE user_id = $1 AND access_token_ciphertext = $2
                RETURNING user_id
                """,
                user_id,
                access_token_ciphertext,
            )
        return cleared is not None

    async def disconnect_github_account(self, *, user_id: str) -> None:
        """Forget the GitHub account, its tokens, and every installation grant."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM agent_repo_grants WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM agent_github_accounts WHERE user_id = $1", user_id)

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
