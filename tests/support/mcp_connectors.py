"""Connector rows for database tests, as an earlier registration left them.

The routes that registered, bound and revoked connectors are retired. A
connection can still name a connector registered before then, and deleting or
re-locating that connection still clears its route, so the tests that cover
those paths seed the rows directly.
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


async def connector_connections(pool: asyncpg.Pool, connector_id: int) -> list[int]:
    """The connection ids one connector is authorized to serve."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT connection_id FROM agent_mcp_connector_connections WHERE connector_id = $1 "
            "ORDER BY connection_id",
            connector_id,
        )
    return [int(row["connection_id"]) for row in rows]
