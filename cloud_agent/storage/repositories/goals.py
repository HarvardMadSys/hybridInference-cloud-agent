"""Postgres implementation of the conversation-goal port.

One subject: the goal a conversation is working toward, its counters, and the
queued automatic rounds that belong to it. Every write here is fenced by the
goal's ``revision``, because two very different writers reach it — an owner
editing an objective, and the platform applying the decision a finished turn
produced — and neither may silently overwrite the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.goals.models import STALLED_AFTER_S, GoalConflict, GoalError
from cloud_agent.storage._sql.rows import thread_goal

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.goals.decision import GoalAction
    from cloud_agent.goals.models import GoalBlockedCode, GoalInput, GoalPauseReason

#: The goal's own columns, read back after every write so the caller always
#: sees what the row now says rather than what it asked for. These reads touch
#: ``agent_threads`` alone, so no alias is needed — unlike the job projections,
#: which join the two tables and must say which revision they mean.
_GOAL_COLUMNS = (
    "goal_objective, goal_state, goal_pause_reason, goal_blocked_code, "
    "goal_blocked_message, goal_revision, goal_max_rounds, goal_rounds_used, "
    "goal_blocked_streak, goal_created_at, goal_updated_at"
)

#: Creating, replacing or editing the goal. Shared with the follow-up path,
#: which accepts a goal alongside a message and has to write it inside the very
#: transaction that appends the turn — the objective a turn ran against and the
#: turn itself must be one commit or neither.
#: ``$2`` objective, ``$3`` max_rounds, ``$4`` rounds_used.
SET_GOAL_SQL = """
UPDATE agent_threads
SET goal_objective = $2,
    goal_state = 'active',
    goal_pause_reason = NULL,
    goal_blocked_code = NULL,
    goal_blocked_message = NULL,
    goal_max_rounds = $3,
    goal_rounds_used = $4,
    goal_blocked_streak = 0,
    goal_revision = goal_revision + 1,
    goal_created_at = COALESCE(goal_created_at, NOW()),
    goal_updated_at = NOW(),
    updated_at = NOW()
WHERE id = $1
"""

#: The columns a caller needs to decide whether the objective changed.
GOAL_STATE_COLUMNS = "goal_objective, goal_rounds_used, goal_revision"


def goal_rounds_used_after(objective_changed: bool, existing_rounds_used: int) -> int:
    """The round counter a new or edited objective starts with.

    The budget restarts only when the objective does. Editing the round cap of a
    goal that has already spent rounds must not hand it a fresh budget as a
    side effect — and replacing the objective must, or a goal that used its
    rounds on the old objective would be born already exhausted.
    """
    return 0 if objective_changed else int(existing_rounds_used or 0)


#: A queued automatic round is the goal's to cancel: it exists only because the
#: goal asked for it, and leaving it in the queue would run one more turn in a
#: conversation whose goal just stopped. Takes a ``text[]`` of thread ids, so
#: one statement serves a single round, a whole conversation, or a page of them.
CANCEL_QUEUED_GOAL_ROUNDS_SQL = """
UPDATE agent_jobs
SET state = 'cancelled', cancel_requested = TRUE,
    terminal_resume_pending = TRUE, updated_at = NOW()
WHERE thread_id = ANY($1::text[]) AND state = 'queued' AND turn_source = 'goal'
"""

#: Archiving a conversation pauses whatever goal was driving it. It is not the
#: owner pausing — nothing was clicked — so the reason records the cause rather
#: than the intent, and "resume" brings it back when the conversation does.
GOAL_ARCHIVE_PAUSE_SQL = """
UPDATE agent_threads t
SET goal_state = 'paused', goal_pause_reason = 'archived',
    goal_blocked_code = NULL, goal_blocked_message = NULL,
    goal_revision = t.goal_revision + 1,
    goal_updated_at = NOW(), updated_at = NOW()
WHERE t.id = ANY($1::text[]) AND t.goal_state = 'active'
"""


def goal_row_to_dict(row: Any) -> dict[str, Any] | None:
    """Decode one ``agent_threads`` goal projection."""
    return thread_goal(row)


class PostgresGoalRepository:
    """Conversation goals: revisions, counters, and the rounds they own."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def get_goal(self, *, thread_id: str) -> dict[str, Any] | None:
        """The conversation's goal row, or ``None`` when it has none."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_GOAL_COLUMNS} FROM agent_threads WHERE id = $1",
                thread_id,
            )
        return None if row is None else goal_row_to_dict(row)

    async def get_goal_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The goal of the conversation the job belongs to, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_GOAL_COLUMNS}
                FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1
                """,
                job_id,
            )
        return None if row is None else goal_row_to_dict(row)

    async def set_goal(
        self,
        *,
        thread_id: str,
        user_id: str,
        goal: GoalInput,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        """Create, replace or edit the goal, returning it or ``None`` if not owned.

        Raises ``GoalConflict`` when ``expected_revision`` no longer matches.
        The revision advances on every accepted write, so a caller that read
        one can prove nothing changed under it. An objective that is unchanged
        keeps the round counter; a different one starts the budget over.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                f"""
                SELECT {_GOAL_COLUMNS}
                FROM agent_threads
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                FOR UPDATE
                """,
                thread_id,
                user_id,
            )
            if current is None:
                return None
            revision = int(current["goal_revision"] or 0)
            if expected_revision is not None and expected_revision != revision:
                raise GoalConflict("This goal changed since you read it. Reload and try again.")
            objective_changed = current["goal_objective"] != goal.objective
            rounds_used = goal_rounds_used_after(
                objective_changed, int(current["goal_rounds_used"] or 0)
            )
            row = await conn.fetchrow(
                f"{SET_GOAL_SQL} RETURNING {_GOAL_COLUMNS}",
                thread_id,
                goal.objective,
                goal.max_rounds,
                rounds_used,
            )
            if objective_changed:
                # The rounds that were queued for the old objective are not
                # rounds for this one.
                await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
        return goal_row_to_dict(row)

    async def pause_goal(self, *, thread_id: str, revision: int, reason: GoalPauseReason) -> bool:
        """``active`` → ``paused``, cancelling queued automatic rounds with it."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_threads
                SET goal_state = 'paused', goal_pause_reason = $3,
                    goal_blocked_code = NULL, goal_blocked_message = NULL,
                    goal_revision = goal_revision + 1,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND goal_revision = $2 AND goal_state = 'active'
                RETURNING id
                """,
                thread_id,
                revision,
                reason,
            )
            if updated is None:
                return False
            await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
        return True

    async def resume_goal(self, *, thread_id: str, revision: int) -> bool:
        """``paused`` → ``active`` and clear the pause reason."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_threads
                SET goal_state = 'active', goal_pause_reason = NULL,
                    goal_blocked_code = NULL, goal_blocked_message = NULL,
                    goal_revision = goal_revision + 1,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND goal_revision = $2 AND goal_state = 'paused'
                RETURNING id
                """,
                thread_id,
                revision,
            )
        return updated is not None

    async def clear_goal(self, *, thread_id: str, user_id: str) -> bool:
        """Remove the goal entirely, cancelling any queued automatic round."""
        async with self._pool.acquire() as conn, conn.transaction():
            cleared = await conn.fetchval(
                """
                UPDATE agent_threads
                SET goal_objective = NULL, goal_state = NULL, goal_pause_reason = NULL,
                    goal_blocked_code = NULL, goal_blocked_message = NULL,
                    goal_max_rounds = NULL, goal_rounds_used = 0, goal_blocked_streak = 0,
                    goal_revision = goal_revision + 1,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                  AND goal_objective IS NOT NULL
                RETURNING id
                """,
                thread_id,
                user_id,
            )
            if cleared is None:
                return False
            await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
        return True

    async def apply_action(
        self,
        *,
        thread_id: str,
        revision: int,
        action: GoalAction,
        pause_reason: GoalPauseReason | None = None,
        blocked_code: GoalBlockedCode | None = None,
    ) -> dict[str, Any] | None:
        """Write what the decision returned, if the goal has not moved since.

        Returns the goal as written, or ``None`` when the revision changed — the
        owner edited, paused or cleared the goal while this turn was finishing,
        and their write is the one that stands. The row is returned rather than
        a flag because a decision can be applied twice in one turn — a blocked
        streak, then the action that streak implies — and the second write has
        to be fenced against the first one's revision.
        """
        state, pause, code, message = _action_columns(action, pause_reason, blocked_code)
        cancel_rounds = action.kind in ("pause", "block", "complete")
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE agent_threads
                SET goal_state = COALESCE($3, goal_state),
                    goal_pause_reason = $4,
                    goal_blocked_code = $5,
                    goal_blocked_message = $6,
                    goal_blocked_streak = $7,
                    goal_revision = CASE WHEN $3::text IS NULL THEN goal_revision
                                         ELSE goal_revision + 1 END,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND goal_revision = $2 AND goal_state IS NOT NULL
                RETURNING {_GOAL_COLUMNS}
                """,
                thread_id,
                revision,
                state,
                pause,
                code,
                message,
                action.blocked_streak,
            )
            if row is None:
                return None
            if cancel_rounds:
                await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
        return goal_row_to_dict(row)

    async def record_goal_report(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        status: str,
        reason: str | None,
    ) -> bool:
        """Store one turn's goal report on its job row, under the live lease.

        The same two columns the message parser writes, so a report made
        through a tool and one written as text are the same fact by the time
        anything reads them. Returns ``False`` when the attempt is no longer
        the running one — a superseded turn must not relabel what its successor
        is doing.
        """
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs j
                SET goal_report = $3, goal_reason = $4, updated_at = NOW()
                WHERE j.id = $1
                  AND j.state = 'running'
                  AND j.current_attempt_id = $2
                  AND EXISTS (
                        SELECT 1 FROM agent_attempts a
                        WHERE a.id = $2 AND a.job_id = j.id
                          AND a.lease_generation = $5
                          AND a.status = 'running'
                          AND a.lease_expires_at > NOW()
                  )
                RETURNING j.id
                """,
                job_id,
                attempt_id,
                status,
                reason,
                lease_generation,
            )
        return updated is not None

    async def has_pending_turn(self, *, thread_id: str) -> bool:
        """Is any turn of this conversation queued or running right now?"""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM agent_jobs "
                    "WHERE thread_id = $1 AND state IN ('queued', 'running'))",
                    thread_id,
                )
            )

    async def pause_stalled_goals(self) -> list[str]:
        """Pause active goals whose conversation has gone quiet; return their ids."""
        async with self._pool.acquire() as conn, conn.transaction():
            stalled = await conn.fetch(
                """
                SELECT t.id
                FROM agent_threads t
                WHERE t.goal_state = 'active'
                  AND t.goal_objective IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_jobs pending
                        WHERE pending.thread_id = t.id
                          AND pending.state IN ('queued', 'running')
                  )
                  AND EXISTS (
                        SELECT 1 FROM agent_jobs recent
                        WHERE recent.thread_id = t.id
                  )
                  AND (
                        SELECT MAX(settled.updated_at) FROM agent_jobs settled
                        WHERE settled.thread_id = t.id
                          AND settled.state IN ('succeeded', 'failed', 'cancelled')
                  ) < NOW() - make_interval(secs => $1)
                ORDER BY t.id
                FOR UPDATE OF t SKIP LOCKED
                """,
                STALLED_AFTER_S,
            )
            thread_ids = [str(row["id"]) for row in stalled]
            if not thread_ids:
                return []
            await conn.execute(
                """
                UPDATE agent_threads
                SET goal_state = 'paused', goal_pause_reason = 'stalled',
                    goal_revision = goal_revision + 1,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE id = ANY($1::text[]) AND goal_state = 'active'
                """,
                thread_ids,
            )
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'cancelled', cancel_requested = TRUE,
                    terminal_resume_pending = TRUE, updated_at = NOW()
                WHERE thread_id = ANY($1::text[]) AND state = 'queued'
                  AND turn_source = 'goal'
                """,
                thread_ids,
            )
        return thread_ids


def _action_columns(
    action: GoalAction,
    pause_reason: GoalPauseReason | None,
    blocked_code: GoalBlockedCode | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """The state columns one of the decision's actions implies.

    A ``None`` state leaves the row's state alone: continuing a goal is not a
    state change, and rewriting ``active`` over an owner's concurrent pause
    would be exactly the lost update the revision fence exists to prevent.
    """
    if action.kind == "pause":
        if pause_reason is None:
            raise GoalError("a pause action must name the reason it paused")
        return "paused", pause_reason, None, None
    if action.kind == "complete":
        return "complete", None, None, None
    if action.kind == "block":
        if blocked_code is None:
            raise GoalError("a block action must name the code it blocked with")
        return "blocked", None, blocked_code, action.message
    return None, None, None, None
