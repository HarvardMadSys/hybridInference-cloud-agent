"""Stopping every piece of one owner's work, as deleting a conversation stops its own.

The writes are the ones conversation deletion makes, applied to all of the
owner's conversations and kept: nothing is deleted, hidden or archived. Queued
turns are cancelled, running ones are asked to stop and reach their terminal
state through the worker's fenced transition at its next heartbeat, waiting
messages and active goals pause, and every residency is released, so no
heartbeat extends it and its host closes the member process. Environment tests
and prebuilds are cancelled the way their owner's Cancel does (a late result is
fenced, the host's orphan cleanup ends the process), and environment actions
are marked stopped so a late start ends its own terminal.

Terminals are a sandbox group's, not a conversation's, so the caller ends them
through each group's sandbox manager. Only groups every one of whose members
is this owner's are returned for that: another owner's member is never
touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.pending_messages import QUEUE_PAUSED_STOPPED
from cloud_agent.storage._sql.goals import PAUSE_GOALS_SQL
from cloud_agent.storage._sql.pending_messages import PAUSE_QUEUE_SQL
from cloud_agent.storage._sql.sandbox import lock_memberships

if TYPE_CHECKING:
    import asyncpg

#: The goal pause detail an owner reads after losing access.
ACCESS_ENDED_DETAIL = "Agent access ended for this account; resume the goal once it is restored"


#: The owner's groups that may still hold processes, and only those whose every
#: member is the owner's. ``workspace_id`` is the member chat the sandbox
#: manager addresses the group by.
_OWNED_LIVE_GROUPS_SQL = """
SELECT g.id AS sandbox_id, g.host, min(m.thread_id) AS workspace_id
FROM agent_sandboxes g
JOIN agent_thread_sandbox_memberships m ON m.sandbox_id = g.id
WHERE g.user_id = $1 AND g.state IN ('starting', 'running')
  AND NOT EXISTS (
      SELECT 1 FROM agent_thread_sandbox_memberships other
      JOIN agent_threads t ON t.id = other.thread_id
      WHERE other.sandbox_id = g.id AND t.user_id <> $1
  )
GROUP BY g.id, g.host
ORDER BY g.id
"""


class PostgresOwnerWorkRepository:
    """Ends one owner's queued, running and resident work."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def end_owner_work(self, user_id: str) -> dict[str, Any]:
        """Cancel queued turns, ask running ones to stop and release residencies.

        Lock order is conversation deletion's: the owner's conversations in id
        order, then their turns, then their memberships in thread order.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            thread_ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM agent_threads WHERE user_id = $1 ORDER BY id FOR UPDATE",
                    user_id,
                )
            ]
            cancelled = await conn.fetch(
                "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                "terminal_resume_pending = TRUE, updated_at = NOW() "
                "WHERE user_id = $1 AND state = 'queued' RETURNING id",
                user_id,
            )
            stopping = await conn.fetch(
                "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                "WHERE user_id = $1 AND state = 'running' AND NOT cancel_requested RETURNING id",
                user_id,
            )
            await conn.execute(PAUSE_QUEUE_SQL, thread_ids, QUEUE_PAUSED_STOPPED)
            await conn.execute(PAUSE_GOALS_SQL, thread_ids, "access_denied", ACCESS_ENDED_DETAIL)
            await lock_memberships(conn, thread_ids)
            await conn.execute(
                "UPDATE agent_environment_runs SET cancel_requested = TRUE, "
                "cleanup_complete = (host IS NULL), state = 'cancelled', finished_at = NOW(), "
                "updated_at = NOW() WHERE user_id = $1 AND state IN ('queued', 'running')",
                user_id,
            )
            await conn.execute(
                "UPDATE agent_environment_action_runs SET state = 'stopped', updated_at = NOW() "
                "WHERE user_id = $1 AND state NOT IN "
                "('succeeded', 'failed', 'closed', 'cancelled', 'stopped', 'unknown')",
                user_id,
            )
            released = await conn.fetch(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = ANY($1::text[]) AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          COALESCE(m.residency_grant_id, '') AS grant_id
                """,
                thread_ids,
            )
            groups = await conn.fetch(_OWNED_LIVE_GROUPS_SQL, user_id)
            running = await conn.fetchval(
                "SELECT count(*) FROM agent_jobs WHERE user_id = $1 AND state = 'running'",
                user_id,
            )
        return {
            "cancelled": len(cancelled),
            "stopping": len(stopping),
            "running": int(running),
            "released_residencies": [dict(row) for row in released],
            "groups": [dict(row) for row in groups],
        }


__all__ = ["ACCESS_ENDED_DETAIL", "PostgresOwnerWorkRepository"]
