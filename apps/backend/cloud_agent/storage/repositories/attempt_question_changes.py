"""Fenced PostgreSQL notification for native pending-question changes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import asyncpg

from cloud_agent.storage._sql.attempts import lock_live_attempt


async def notify_question_change(
    pool: asyncpg.Pool,
    lock_live_attempt: Callable[[Any, int, int], Awaitable[bool]],
    *,
    job_id: str,
    attempt_id: int,
    lease_generation: int,
) -> bool:
    """Publish a thread hint while holding the live attempt's fence."""
    async with pool.acquire() as conn, conn.transaction():
        if not await lock_live_attempt(conn, attempt_id, lease_generation):
            return False
        row = await conn.fetchrow(
            """
            SELECT j.user_id, t.id AS thread_id
            FROM agent_jobs j
            JOIN agent_attempts a ON a.job_id = j.id
            JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
            WHERE j.id = $1 AND j.current_attempt_id = $2 AND j.state = 'running'
              AND a.id = $2 AND a.lease_generation = $3 AND a.status = 'running'
              AND a.lease_expires_at > clock_timestamp() AND t.deleted_at IS NULL
            """,
            job_id,
            attempt_id,
            lease_generation,
        )
        if row is None:
            return False
        await conn.execute(
            "SELECT pg_notify('agent_owner_changes', $1)",
            json.dumps(
                {"user": row["user_id"], "kind": "question", "id": row["thread_id"]},
                separators=(",", ":"),
            ),
        )
    return True


class AttemptQuestionChanges:
    """Mixin for the attempt repository's fenced owner-question notification."""

    _pool: asyncpg.Pool

    @staticmethod
    async def _lock_live_attempt(conn: Any, attempt_id: int, lease_generation: int | None) -> bool:
        return await lock_live_attempt(conn, attempt_id, lease_generation) is not None

    async def notify_question_change(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> bool:
        """Notify owners of a question change if this attempt still holds its lease."""
        return await notify_question_change(
            self._pool,
            self._lock_live_attempt,
            job_id=job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
        )
