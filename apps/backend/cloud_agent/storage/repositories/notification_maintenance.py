"""Postgres retention for durable owner notification markers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


async def _prune_notification_changes(conn: Any, *, limit: int) -> int:
    """Delete one batch and advance every affected owner's cursor floor."""
    async with conn.transaction(isolation="read_committed"):
        await conn.execute("SET LOCAL statement_timeout = '5s'")
        removed = await conn.fetchval(
            """
            WITH expired AS (
                SELECT kind,entity_id FROM agent_notification_changes
                WHERE changed_at<NOW()-interval '24 hours'
                ORDER BY changed_at LIMIT $1 FOR UPDATE SKIP LOCKED
            ), removed AS (
                DELETE FROM agent_notification_changes c USING expired e
                WHERE c.kind=e.kind AND c.entity_id=e.entity_id
                RETURNING c.user_id
            ), floors AS (
                INSERT INTO agent_notification_change_floors (user_id,floor_xid)
                SELECT user_id,pg_current_xact_id()
                FROM (SELECT DISTINCT user_id FROM removed) affected
                ORDER BY user_id
                ON CONFLICT (user_id) DO UPDATE SET floor_xid=EXCLUDED.floor_xid
                RETURNING user_id
            )
            SELECT count(*) FROM removed
            """,
            limit,
        )
    return 0 if removed is None else int(removed)


class PostgresNotificationMaintenanceRepository:
    """Retain notification markers without coupling cleanup to owner reads."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def prune_notification_changes(self, *, limit: int = 500) -> int:
        """Delete one bounded batch of expired markers."""
        if limit <= 0:
            raise ValueError("notification prune limit must be positive")
        async with self._pool.acquire() as conn:
            return await _prune_notification_changes(conn, limit=limit)
