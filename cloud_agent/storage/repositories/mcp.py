"""Postgres implementation of the MCP-connection port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


class PostgresMcpConnectionRepository:
    """Per-user MCP connections: one credential per person per server."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return this user's MCP connections, including encrypted tokens.

        Ordered by server name so the composer and the Integrations page agree
        on order without either sorting again.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, server, account_label, access_token_ciphertext,
                       refresh_token_ciphertext, expires_at, created_at, updated_at
                FROM agent_mcp_connections WHERE user_id = $1 ORDER BY server
                """,
                user_id,
            )
        return [dict(row) for row in rows]

    async def upsert_mcp_connection(
        self,
        *,
        user_id: str,
        server: str,
        account_label: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str | None,
        expires_at: Any,
    ) -> int:
        """Store one credential per user per server, replacing any earlier one.

        Reconnecting is an update rather than a second row: "connected" is a
        yes/no per person per server, and two live credentials for one pair
        would leave which one a job used down to row order.
        """
        async with self._pool.acquire() as conn:
            connection_id = await conn.fetchval(
                """
                INSERT INTO agent_mcp_connections
                    (user_id, server, account_label, access_token_ciphertext,
                     refresh_token_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id, server) DO UPDATE SET
                    account_label = EXCLUDED.account_label,
                    access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                    refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING id
                """,
                user_id,
                server,
                account_label,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return int(connection_id)

    async def delete_mcp_connection(self, *, user_id: str, server: str) -> bool:
        """Disconnect one server for this user; ``False`` if it was not connected."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_mcp_connections WHERE user_id = $1 AND server = $2 RETURNING id",
                user_id,
                server,
            )
        return deleted is not None
