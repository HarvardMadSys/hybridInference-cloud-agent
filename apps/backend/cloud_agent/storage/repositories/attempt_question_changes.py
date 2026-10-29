"""Fenced PostgreSQL notification for native pending-question changes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

from cloud_agent.storage._sql.attempts import lock_live_attempt


async def notify_question_change(
    pool: asyncpg.Pool,
    *,
    job_id: str,
    attempt_id: int,
    lease_generation: int,
) -> bool:
    """Publish a thread hint while holding the live attempt's fence."""
    async with pool.acquire() as conn, conn.transaction():
        locked_job_id = await lock_live_attempt(conn, attempt_id, lease_generation)
        if locked_job_id != job_id:
            return False
        row = await conn.fetchrow(
            """
            SELECT j.user_id, j.current_attempt_id, j.state, t.id AS thread_id, t.deleted_at
            FROM agent_jobs j
            JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
            WHERE j.id = $1
            """,
            locked_job_id,
        )
        if row is None:
            return False
        # Deleting a conversation marks its thread deleted before the active
        # worker can finish through its lease fence. Treat a pending question's
        # final change notification as a no-op so it cannot abort that finish.
        if row["deleted_at"] is not None:
            return True
        if row["current_attempt_id"] != attempt_id or row["state"] != "running":
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

    async def notify_question_change(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> bool:
        """Notify owners of a question change if this attempt still holds its lease."""
        return await notify_question_change(
            self._pool, job_id=job_id, attempt_id=attempt_id, lease_generation=lease_generation
        )
