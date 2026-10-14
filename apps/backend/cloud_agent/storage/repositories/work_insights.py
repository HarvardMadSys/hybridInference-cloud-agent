"""Postgres reads for the home page's work insights."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class PostgresWorkInsightsRepository:
    """The owner's own recent requests; nothing is written."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool."""
        self._pool = pool

    async def recent_requests(self, user_id: str, limit: int = 50) -> list[str]:
        """The owner's latest requests, newest first.

        Only messages the owner wrote count: not assistant replies, forked
        copies, automatic goal turns, or messages in deleted conversations.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT left(m.content, 8000) AS content
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id AND j.thread_id = m.thread_id
                JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = j.user_id
                WHERE j.user_id = $1 AND t.deleted_at IS NULL
                  AND m.role = 'user' AND j.fork_source_job_id IS NULL
                  AND (j.turn_source <> 'goal' OR m.mid_turn)
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT $2
                """,
                user_id,
                min(max(limit, 0), 50),
            )
        return [str(row["content"]) for row in rows]
