"""Connector rows for migration tests, as an earlier registration left them.

No connector ever ran, and nothing in the application reads or writes these
tables any more. The migrations that repaired and then retired connector
routes still have to be tested over the rows a release before them wrote, so
those tests seed them directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


async def insert_connector(
    pool: asyncpg.Pool, *, user_id: str, name: str, revoked: bool = False
) -> dict[str, Any]:
    """Insert one connector, pending or already revoked."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_mcp_connectors (user_id, name, registration_hash, status, revoked_at)
            VALUES ($1, $2, $3, $4, CASE WHEN $4 = 'revoked' THEN NOW() END)
            RETURNING id, user_id, name, status
            """,
            user_id,
            name,
            f"registration-{user_id}-{name}",
            "revoked" if revoked else "pending",
        )
    return dict(row)
