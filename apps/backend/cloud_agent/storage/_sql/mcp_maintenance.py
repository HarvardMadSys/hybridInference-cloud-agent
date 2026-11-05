"""Bounded keyset pages for remembered MCP approval maintenance."""

from typing import Any

ALLOW_GROUP_PAGE = 50


async def pending_allow_groups(
    conn: Any,
    *,
    thread_id: str | None,
    connection_id: int | None,
    after: tuple[str, int] | None,
) -> list[Any]:
    """Page background work; explicit thread/connection flushes retain their scope."""
    return list(
        await conn.fetch(
            """
        SELECT DISTINCT thread_id, connection_id FROM agent_mcp_invocations
        WHERE always_allow_status = 'pending'
          AND ($1::text IS NULL OR thread_id = $1)
          AND ($2::bigint IS NULL OR connection_id = $2)
          AND ($3::text IS NULL OR (thread_id, connection_id) > ($3, $4::bigint))
        ORDER BY thread_id, connection_id
        LIMIT $5
        """,
            thread_id,
            connection_id,
            after[0] if after else None,
            after[1] if after else None,
            ALLOW_GROUP_PAGE if thread_id is None and connection_id is None else None,
        )
    )
