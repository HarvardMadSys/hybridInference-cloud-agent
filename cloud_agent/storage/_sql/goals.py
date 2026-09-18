"""Statements that keep a goal's state honest when a turn ends off the happy path.

A turn can stop being workable in several places that are not the ordinary
finish handler: it can be cancelled while it waits in the queue, its lease can
expire and be reaped, or its conversation's native context can be lost and take
every queued descendant with it. Each of those runs inside someone else's
transaction, so what they share lives here as one statement rather than as
three copies of the same rule.

Every one of them *pauses*. None of them continues, completes or blocks: a goal
whose turn did not end workably stops and waits for its owner, which is the only
answer that cannot spend someone's money on a conversation that is broken.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

#: Pause an active goal behind one thread, in the caller's transaction.
#: ``$1`` is the thread id and ``$2`` the reason. A goal that is not active is
#: left exactly as it is — a paused goal stays paused for the reason it was
#: paused, and a complete one is not un-completed by a late reaping pass.
PAUSE_GOAL_SQL = """
UPDATE agent_threads
SET goal_state = 'paused', goal_pause_reason = $2,
    goal_blocked_code = NULL, goal_blocked_message = NULL,
    goal_revision = goal_revision + 1,
    goal_updated_at = NOW(), updated_at = NOW()
WHERE id = $1 AND goal_state = 'active'
"""

#: The same rule for a caller that has more than one thread at a time: the
#: archive page, and the sweep that fails a queued continuation when its
#: conversation's native context is gone.
PAUSE_GOALS_SQL = """
UPDATE agent_threads t
SET goal_state = 'paused', goal_pause_reason = $2,
    goal_blocked_code = NULL, goal_blocked_message = NULL,
    goal_revision = t.goal_revision + 1,
    goal_updated_at = NOW(), updated_at = NOW()
WHERE t.id = ANY($1::text[]) AND t.goal_state = 'active'
RETURNING t.id
"""


async def pause_goal(conn: Any, *, thread_id: str, reason: str) -> bool:
    """Pause one thread's active goal inside the caller's transaction."""
    updated = await conn.fetchval(PAUSE_GOAL_SQL, thread_id, reason)
    return updated is not None


async def goal_after_terminal(conn: Any, *, job_id: str, state: str) -> bool:
    """Pause the goal behind a job this caller has just made terminal.

    Read from the job row rather than taken on the caller's word, so a turn
    cannot be described as something other than what the database says it is.
    Only an automatic round pauses a goal this way: a person's own turn failing
    is reported to the person, and is not by itself a reason to stop their goal.
    """
    reason = "cancelled" if state == "cancelled" else "turn-failed"
    return bool(
        await conn.fetchval(
            """
            UPDATE agent_threads t
            SET goal_state = 'paused', goal_pause_reason = $2,
                goal_blocked_code = NULL, goal_blocked_message = NULL,
                goal_revision = t.goal_revision + 1,
                goal_updated_at = NOW(), updated_at = NOW()
            WHERE t.id = (SELECT j.thread_id FROM agent_jobs j WHERE j.id = $1)
              AND t.goal_state = 'active'
              AND EXISTS (
                    SELECT 1 FROM agent_jobs j
                    WHERE j.id = $1 AND j.turn_source = 'goal'
              )
            RETURNING t.id
            """,
            job_id,
            reason,
        )
    )
