"""Postgres implementation of the conversation-goal port.

One subject: the goal a conversation is working toward, its counters, and the
queued automatic rounds that belong to it. Every write here is fenced by the
goal's ``revision``, because two very different writers reach it — an owner
editing an objective, and the platform applying the decision a finished turn
produced — and neither may silently overwrite the other.

The other half of this repository is the *scheduling obligation*. Any write that
leaves an active goal with something to do also records a wakeup, in the same
transaction. That is what removes the window between "this turn ended" and "the
next turn exists" in which a crash used to lose a goal forever.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from cloud_agent.goals.models import (
    ARM_AFTER_S,
    MAX_REASON_CHARS,
    STALLED_AFTER_S,
    GoalConflict,
)
from cloud_agent.settings import number_setting
from cloud_agent.storage._sql.goals import (
    CANCEL_QUEUED_GOAL_ROUNDS_SQL,
    EDITED_WHILE_PAUSED_DETAIL,
    PAUSE_GOALS_SQL,
    REPAIRS_USED_SQL,
    ROUNDS_USED_SQL,
    SET_GOAL_SQL,
    SPENT_ROUNDS_SQL,
)
from cloud_agent.storage._sql.rows import thread_goal

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.goals.decision import GoalAction
    from cloud_agent.goals.models import GoalInput, GoalPauseReason

#: The goal's own columns, read back after every write so the caller always
#: sees what the row now says rather than what it asked for. Written once as
#: names because there are two projections of them and they must not drift.
_GOAL_COLUMN_NAMES = (
    "goal_id",
    "goal_objective",
    "goal_state",
    "goal_pause_reason",
    "goal_pause_detail",
    "goal_revision",
    "goal_max_rounds",
    "goal_completion_basis",
    "goal_completed_by_job_id",
    "goal_created_at",
    "goal_updated_at",
)

#: For statements whose ``FROM`` holds ``agent_threads`` alone: ``get_goal``,
#: and the ``RETURNING`` of ``SET_GOAL_SQL`` and ``apply_action``. There is no
#: second table for a name to be ambiguous with, and qualifying here would be a
#: syntax error — a ``RETURNING`` list cannot carry an alias the ``UPDATE`` did
#: not introduce.
_GOAL_COLUMNS = ", ".join(_GOAL_COLUMN_NAMES)

#: For reads that join. ``0019`` put ``goal_revision`` on ``agent_jobs`` as well
#: — the revision *that turn* was created for — so an unqualified name is
#: ambiguous there, and Postgres refuses the whole statement (42702) rather than
#: picking one.
_GOAL_COLUMNS_QUALIFIED = ", ".join(f"t.{name}" for name in _GOAL_COLUMN_NAMES)

#: Everything a caller needs to decide what a write means, with the two derived
#: counters, formatted for a read that has ``agent_threads t`` in scope.
_GOAL_READ_COLUMNS = f"{_GOAL_COLUMNS}, {ROUNDS_USED_SQL}, {REPAIRS_USED_SQL}"
_GOAL_READ_COLUMNS_QUALIFIED = f"{_GOAL_COLUMNS_QUALIFIED}, {ROUNDS_USED_SQL}, {REPAIRS_USED_SQL}"

#: The columns a caller needs to decide whether the objective changed, and
#: whether the goal being set is one that was already complete.
GOAL_STATE_COLUMNS = (
    "goal_id, goal_objective, goal_state, goal_revision, goal_max_rounds, "
    "goal_pause_reason, goal_pause_detail"
)


def new_goal_id() -> str:
    """A fresh goal identity. Opaque and never reused."""
    return uuid.uuid4().hex


def goal_row_to_dict(row: Any) -> dict[str, Any] | None:
    """Decode one ``agent_threads`` goal projection."""
    return thread_goal(row)


class PostgresGoalRepository:
    """Conversation goals: identities, revisions, and the rounds they own."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    # -- reads --------------------------------------------------------------
    async def get_goal(self, *, thread_id: str, conn: Any | None = None) -> dict[str, Any] | None:
        """The conversation's goal row, or ``None`` when it has none.

        ``conn`` runs the read inside a transaction the caller already owns,
        which is what lets a decision re-read the goal under the same lock it
        writes under.
        """
        if conn is not None:
            row = await conn.fetchrow(
                f"SELECT {_GOAL_READ_COLUMNS} FROM agent_threads t WHERE t.id = $1",
                thread_id,
            )
            return None if row is None else goal_row_to_dict(row)
        async with self._pool.acquire() as acquired:
            row = await acquired.fetchrow(
                f"SELECT {_GOAL_READ_COLUMNS} FROM agent_threads t WHERE t.id = $1",
                thread_id,
            )
        return None if row is None else goal_row_to_dict(row)

    async def lock_goal_state(self, *, thread_id: str, conn: Any) -> dict[str, Any] | None:
        """Read the goal inside the caller's transaction, taking its row lock."""
        row = await conn.fetchrow(
            f"SELECT {_GOAL_READ_COLUMNS} FROM agent_threads t WHERE t.id = $1 FOR UPDATE OF t",
            thread_id,
        )
        return None if row is None else goal_row_to_dict(row)

    async def live_turn_id(self, *, thread_id: str, conn: Any | None = None) -> str | None:
        """The id of the turn that is queued or running right now, if any."""
        sql = (
            "SELECT id FROM agent_jobs WHERE thread_id = $1 "
            "AND state IN ('queued', 'running') "
            "ORDER BY CASE WHEN state = 'running' THEN 0 ELSE 1 END, turn_no DESC LIMIT 1"
        )
        if conn is not None:
            return await conn.fetchval(sql, thread_id)
        async with self._pool.acquire() as acquired:
            return await acquired.fetchval(sql, thread_id)

    async def get_goal_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The goal of the conversation the job belongs to, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_GOAL_READ_COLUMNS_QUALIFIED}
                FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1
                """,
                job_id,
            )
        return None if row is None else goal_row_to_dict(row)

    async def get_thread_goal_state(self, *, user_id: str, thread_id: str) -> dict[str, Any] | None:
        """A conversation's goal *and* its control version, owner-checked.

        ``None`` when the conversation is not this owner's. A conversation with
        no goal answers with ``goal=None`` and the revision it is at, because
        the next write has to be compared against something.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_GOAL_READ_COLUMNS}
                FROM agent_threads t
                WHERE t.id = $1 AND t.user_id = $2 AND t.deleted_at IS NULL
                """,
                thread_id,
                user_id,
            )
        if row is None:
            return None
        return {"goal": goal_row_to_dict(row), "revision": int(row["goal_revision"] or 0)}

    async def has_pending_turn(self, *, thread_id: str, conn: Any | None = None) -> bool:
        """Is any turn of this conversation queued or running right now?"""
        sql = (
            "SELECT EXISTS (SELECT 1 FROM agent_jobs "
            "WHERE thread_id = $1 AND state IN ('queued', 'running'))"
        )
        if conn is not None:
            return bool(await conn.fetchval(sql, thread_id))
        async with self._pool.acquire() as acquired:
            return bool(await acquired.fetchval(sql, thread_id))

    # -- owner writes -------------------------------------------------------
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
        Replacing the objective mints a new ``goal_id`` and starts the round
        count over; editing only the bound keeps both. A completed goal set again
        is a new authorization and mints a new identity even when the text is
        identical. A paused goal that is edited stays paused.

        ``goal.max_rounds`` is tri-state: omitted leaves whatever bound the goal
        already carries untouched, a number sets one, and an explicit ``null``
        lifts one. A writer reads :attr:`GoalInput.states_max_rounds` rather than
        the field, or an edit that only rewrites the objective would delete a
        limit its owner stated.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                f"""
                SELECT {GOAL_STATE_COLUMNS}
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
            # A goal that had finished is being authorized again, even though
            # its words are unchanged: the last authorization was spent, and
            # reusing its identity would make an old report look current.
            reauthorized = current["goal_state"] == "complete"
            # A paused goal that is edited stays paused, whatever was edited.
            # The owner stopped it; only the owner starts it again.
            state = "paused" if current["goal_state"] == "paused" else "active"
            await conn.execute(
                SET_GOAL_SQL,
                thread_id,
                goal.objective,
                goal.max_rounds,
                new_goal_id(),
                state,
                reauthorized,
                EDITED_WHILE_PAUSED_DETAIL,
                # Absence is not ``None``: a request that says nothing about the
                # bound leaves the owner's alone, and only an explicit ``null``
                # lifts it. Reading the field instead would make every edit that
                # rewrites the objective a silent removal.
                goal.states_max_rounds,
            )
            # A queued round belongs to the objective it was written for. Both
            # paths that replace an objective — this one and the follow-up
            # append — cancel it, or the next claim would run a prompt about the
            # goal the owner just replaced.
            await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
            written = await conn.fetchrow(
                f"SELECT {_GOAL_READ_COLUMNS} FROM agent_threads t WHERE t.id = $1",
                thread_id,
            )
            if state == "active" and written is not None:
                # The set is the owner's authorization: record that this goal
                # has to be looked at, in the transaction that created it.
                await _arm(
                    conn,
                    thread_id=thread_id,
                    goal_id=str(written["goal_id"]),
                    revision=int(written["goal_revision"]),
                    trigger="owner",
                    trigger_ref=f"set:{written['goal_revision']}",
                )
        return goal_row_to_dict(written)

    async def pause_goal(
        self,
        *,
        thread_id: str,
        revision: int,
        reason: GoalPauseReason,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        """``active`` → ``paused``, cancelling queued automatic rounds with it.

        A pause never interrupts a turn that is already running: the owner's
        "stop this" is the stop button, and this is "do not start another one".
        """
        async with self._pool.acquire() as conn, conn.transaction():
            written = await conn.fetchrow(
                """
                UPDATE agent_threads
                SET goal_state = 'paused', goal_pause_reason = $3,
                    goal_pause_detail = $4,
                    goal_completion_basis = NULL, goal_completed_by_job_id = NULL,
                    goal_revision = goal_revision + 1,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND goal_revision = $2 AND goal_state = 'active'
                RETURNING id
                """,
                thread_id,
                revision,
                reason,
                detail if detail is not None else _pause_detail(reason),
            )
            if written is None:
                return None
            await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
            # The rounds that were queued for this goal are gone, so any wakeup
            # that was going to schedule one is void: leaving it pending would
            # let a consumer resurrect a goal the owner just stopped.
            await conn.execute(
                "UPDATE agent_goal_wakeups SET state = 'processed', detail = 'goal paused', "
                "lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW() "
                "WHERE thread_id = $1 AND state = 'pending'",
                thread_id,
            )
            row = await conn.fetchrow(
                f"SELECT {_GOAL_READ_COLUMNS} FROM agent_threads t WHERE t.id = $1", thread_id
            )
        return goal_row_to_dict(row)

    async def resume_goal(
        self, *, thread_id: str, user_id: str, revision: int
    ) -> dict[str, Any] | None:
        """``paused`` → ``active``, and record the obligation to schedule.

        Returns the goal as written, or ``None`` when the revision moved under
        the caller. The owner's authorization is the trigger: no model report is
        needed to start working again.

        **Resuming releases a bound that has been spent.** A goal is only ever
        bounded because the owner stated a number, and a goal sitting at that
        number cannot be started again while it still applies — the very next
        decision would read the same fact and pause it straight back, which
        makes the only Resume control in the product a no-op. There is no other
        way out: the round bound has no configuration surface. So the owner's
        own "continue" is what lifts it, the new bound travels back in the
        response, and the goal continues unbounded until it finishes or stops
        for one of the reasons that still apply.

        A bound that has *not* been reached is left exactly as it was: this is
        not a general amnesty, and an owner who said "at most fifty" and paused
        at three still gets fifty.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            written = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET goal_state = 'active', goal_pause_reason = NULL,
                    goal_pause_detail = NULL,
                    goal_max_rounds = CASE
                        WHEN t.goal_max_rounds IS NOT NULL
                             AND t.goal_max_rounds <= """
                + SPENT_ROUNDS_SQL
                + """
                        THEN NULL
                        ELSE t.goal_max_rounds
                    END,
                    goal_completion_basis = NULL, goal_completed_by_job_id = NULL,
                    goal_revision = goal_revision + 1,
                    goal_updated_at = NOW(), updated_at = NOW()
                WHERE t.id = $1 AND t.user_id = $2 AND t.goal_revision = $3
                  AND t.goal_state = 'paused'
                RETURNING t.id, t.goal_id, t.goal_revision
                """,
                thread_id,
                user_id,
                revision,
            )
            if written is None:
                return None
            await conn.execute(
                """
                UPDATE agent_goal_wakeups
                SET state = 'processed', detail = 'superseded by resume',
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW()
                WHERE thread_id = $1 AND state = 'pending'
                """,
                thread_id,
            )
            await _arm(
                conn,
                thread_id=thread_id,
                goal_id=str(written["goal_id"]),
                revision=int(written["goal_revision"]),
                trigger="owner",
                trigger_ref=f"resume:{written['goal_revision']}",
            )
            row = await conn.fetchrow(
                f"SELECT {_GOAL_READ_COLUMNS} FROM agent_threads t WHERE t.id = $1", thread_id
            )
        return goal_row_to_dict(row)

    async def clear_goal(self, *, thread_id: str, user_id: str) -> bool:
        """Remove the goal entirely, cancelling any queued automatic round."""
        async with self._pool.acquire() as conn, conn.transaction():
            cleared = await conn.fetchval(
                """
                UPDATE agent_threads
                SET goal_objective = NULL, goal_id = NULL, goal_state = NULL,
                    goal_pause_reason = NULL, goal_pause_detail = NULL,
                    goal_max_rounds = NULL, goal_repairs_used = 0,
                    goal_completion_basis = NULL, goal_completed_by_job_id = NULL,
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
            await conn.execute(
                "UPDATE agent_goal_wakeups SET state = 'processed', detail = 'goal cleared', "
                "lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW() "
                "WHERE thread_id = $1 AND state = 'pending'",
                thread_id,
            )
        return True

    # -- the decision's write ----------------------------------------------
    async def apply_action(
        self,
        *,
        thread_id: str,
        revision: int,
        action: GoalAction,
        pause_reason: GoalPauseReason | None = None,
        pause_detail: str | None = None,
        completed_by_job_id: str | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        """Write what the decision returned, if the goal has not moved since.

        Returns the goal as written, or ``None`` when the revision changed — the
        owner edited, paused or cleared the goal while this turn was finishing,
        and their write is the one that stands.
        """
        state, reason, detail, basis, completed_by = _action_columns(
            action, pause_reason, pause_detail, completed_by_job_id
        )
        cancel_rounds = action.kind in ("pause", "complete")
        if conn is not None:
            return await self._apply_action_on(
                conn, thread_id, revision, state, reason, detail, basis, completed_by, cancel_rounds
            )
        async with self._pool.acquire() as acquired, acquired.transaction():
            return await self._apply_action_on(
                acquired,
                thread_id,
                revision,
                state,
                reason,
                detail,
                basis,
                completed_by,
                cancel_rounds,
            )

    async def _apply_action_on(
        self,
        conn: Any,
        thread_id: str,
        revision: int,
        state: str | None,
        reason: str | None,
        detail: str | None,
        basis: str | None,
        completed_by: str | None,
        cancel_rounds: bool,
    ) -> dict[str, Any] | None:
        """The one statement, on whichever connection owns the transaction."""
        row = await conn.fetchrow(
            f"""
            UPDATE agent_threads
            SET goal_state = COALESCE($3, goal_state),
                -- Why it stopped belongs to the stop. A ``none`` decision
                -- exists to record that a turn was judged and nothing
                -- followed from it, and clearing the reason on the way past
                -- left a paused goal that could not say why.
                goal_pause_reason = CASE WHEN $3::text IS NULL
                                         THEN goal_pause_reason ELSE $4 END,
                goal_pause_detail = CASE WHEN $3::text IS NULL
                                         THEN goal_pause_detail ELSE $5 END,
                goal_completion_basis = CASE WHEN $3::text IS NULL
                                             THEN goal_completion_basis ELSE $6 END,
                goal_completed_by_job_id = CASE WHEN $3::text IS NULL
                                                THEN goal_completed_by_job_id ELSE $7 END,
                goal_revision = CASE WHEN $3::text IS NULL THEN goal_revision
                                     ELSE goal_revision + 1 END,
                goal_updated_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND goal_revision = $2 AND goal_state IS NOT NULL
            RETURNING {_GOAL_COLUMNS}
            """,
            thread_id,
            revision,
            state,
            reason,
            detail,
            basis,
            completed_by,
        )
        if row is None:
            return None
        if cancel_rounds:
            await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
            await conn.execute(
                "UPDATE agent_goal_wakeups SET state = 'processed', "
                "detail = $2, lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = NOW() "
                "WHERE thread_id = $1 AND state = 'pending'",
                thread_id,
                f"goal {state}",
            )
        return goal_row_to_dict(row)

    # -- the finished turn's report ----------------------------------------
    # -- recovery -----------------------------------------------------------
    async def sweep_goal_scheduling(self) -> dict[str, list[str]]:
        """Recover goals whose scheduling obligation was lost, and pause the rest.

        Three shapes are looked for, in this order:

        * ``rearmed`` — an ``active`` goal whose parked wakeup is no longer
          waiting on anything, or whose conversation has gone idle with no
          wakeup at all. The decision is made again.
        * ``stalled`` — an ``active`` goal with nothing queued, running or
          pending for longer than :data:`STALLED_AFTER_S`. No path can explain
          it, so it is paused and said so.

        Returns the thread ids by shape, for the caller's log line.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # A wakeup parked behind a turn that has since settled is due, and
            # may never have been released: registering the wait is a second
            # write, so the turn can finish between the read that chose it and
            # the write that names it. This is where that lost race is repaired,
            # and it is deliberately unconditional — a pending wakeup is not a
            # reason to leave a settled wait alone.
            unparked = await conn.fetch(
                """
                UPDATE agent_goal_wakeups w
                SET waiting_job_id = NULL, updated_at = NOW()
                FROM agent_jobs blocker
                WHERE blocker.id = w.waiting_job_id
                  AND blocker.state IN ('succeeded', 'failed', 'cancelled')
                  AND w.state = 'pending'
                RETURNING w.id
                """
            )
            rearm = await conn.fetch(
                """
                SELECT t.id AS thread_id, t.goal_id, t.goal_revision
                FROM agent_threads t
                WHERE t.goal_state = 'active' AND t.goal_objective IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_jobs pending
                        WHERE pending.thread_id = t.id
                          AND pending.state IN ('queued', 'running')
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_goal_wakeups w
                        WHERE w.thread_id = t.id AND w.state = 'pending'
                  )
                  AND t.goal_updated_at > clock_timestamp() - make_interval(secs => $1)
                ORDER BY t.goal_updated_at
                LIMIT 200
                """,
                number_setting("goals.arm_after_seconds", ARM_AFTER_S),
            )
            # The ids of wakeups this pass made due again. Populated before the
            # arming loop below, which appends the goals it newly armed.
            rearmed: list[str] = [str(row["id"]) for row in unparked]
            for row in rearm:
                # An ``active`` goal with nothing queued, running or pending, and
                # a write recent enough that a consumer may still be about to
                # act on it. Arming is idempotent, so a recovery pass racing a
                # live consumer costs one INSERT that does nothing.
                await _arm(
                    conn,
                    thread_id=str(row["thread_id"]),
                    goal_id=str(row["goal_id"]),
                    revision=int(row["goal_revision"]),
                    trigger="owner",
                    trigger_ref=f"recovery:{row['goal_revision']}",
                )
                rearmed.append(str(row["thread_id"]))
            # A queued automatic round that no longer matches its goal can never
            # be claimed — the claim statement re-checks the identity — so it
            # would wait in the queue forever and make its conversation look
            # busy. Cancelling it here is what keeps that check honest.
            cancelled = await conn.fetch(
                """
                UPDATE agent_jobs j
                SET state = 'cancelled', cancel_requested = TRUE,
                    terminal_resume_pending = TRUE, updated_at = NOW()
                FROM agent_threads t
                WHERE j.thread_id = t.id AND j.state = 'queued'
                  AND j.turn_source = 'goal'
                  AND (
                        t.goal_state IS DISTINCT FROM 'active'
                        OR t.goal_id IS DISTINCT FROM j.goal_id
                        OR t.goal_revision IS DISTINCT FROM j.goal_revision
                  )
                RETURNING j.thread_id, t.goal_state, t.goal_id, t.goal_revision
                """
            )
            for row in cancelled:
                if row["goal_state"] != "active" or row["goal_id"] is None:
                    continue
                # The goal is still live and still authorized; only the round
                # was stale. Record a fresh obligation so the decision is made
                # again against the revision that stands now.
                await _arm(
                    conn,
                    thread_id=str(row["thread_id"]),
                    goal_id=str(row["goal_id"]),
                    revision=int(row["goal_revision"]),
                    trigger="owner",
                    trigger_ref=f"stale-round:{row['goal_revision']}",
                )
                rearmed.append(str(row["thread_id"]))
            stalled = await conn.fetch(
                """
                SELECT t.id
                FROM agent_threads t
                WHERE t.goal_state = 'active' AND t.goal_objective IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_jobs pending
                        WHERE pending.thread_id = t.id
                          AND pending.state IN ('queued', 'running')
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_goal_wakeups w
                        WHERE w.thread_id = t.id AND w.state = 'pending'
                  )
                  AND t.goal_updated_at < clock_timestamp() - make_interval(secs => $1)
                ORDER BY t.goal_updated_at
                LIMIT 200
                FOR UPDATE OF t SKIP LOCKED
                """,
                number_setting("goals.stalled_after_seconds", STALLED_AFTER_S),
            )
            stalled_ids = [str(row["id"]) for row in stalled]
            if stalled_ids:
                await conn.execute(
                    PAUSE_GOALS_SQL,
                    stalled_ids,
                    "scheduling_failed",
                    "the platform stopped scheduling this goal and could not "
                    "explain why; resume it to try again",
                )
        return {"rearmed": rearmed, "stalled": stalled_ids}


def _pause_detail(reason: str) -> str:
    """The user-readable half of a pause the *platform* decided.

    An owner's own pause needs no explanation. Every other reason is something
    the owner has to be told, in words, or the badge is a dead end.
    """
    return {
        "user": "paused by the user",
        "fork": "copied from another conversation; resume it to start working",
        "needs_input": "the agent stopped and asked for input",
        "round_limit": "the goal reached the round limit its owner set",
        "execution_failed": "the automatic round that was running did not finish",
        "cancelled": "the automatic round was cancelled",
        "report_invalid": "the agent's completion report could not be read",
        "report_unavailable": "the agent's final message could not be read",
        "runtime_unsupported": "this session's runtime cannot report goal progress",
        "context_lost": "the conversation's session is gone",
        "access_denied": "this conversation no longer has access to its repository",
        "quota_unavailable": "the account's inference quota is not available",
        "scheduling_failed": "the platform could not schedule the next round",
        "archived": "the conversation was archived",
    }.get(reason, reason)


def _action_columns(
    action: GoalAction,
    pause_reason: GoalPauseReason | None,
    pause_detail: str | None,
    completed_by_job_id: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """The state columns one of the decision's actions implies.

    A ``None`` state leaves the row's state alone: continuing a goal is not a
    state change, and rewriting ``active`` over an owner's concurrent pause
    would be exactly the lost update the revision fence exists to prevent.
    """
    if action.kind == "pause":
        reason = pause_reason or action.reason
        detail = (pause_detail or action.message or _pause_detail(reason or ""))[:MAX_REASON_CHARS]
        return "paused", reason, detail, None, None
    if action.kind == "complete":
        # A completion names the turn that earned it. Without one there is
        # nothing to show the owner, and the shape constraint refuses the row.
        return (
            "complete",
            None,
            None,
            action.completion_basis or "model_reported",
            completed_by_job_id,
        )
    return None, None, None, None, None


async def _arm(
    conn: Any,
    *,
    thread_id: str,
    goal_id: str | None,
    revision: int,
    trigger: str,
    trigger_ref: str,
    waiting_job_id: str | None = None,
) -> str | None:
    """Insert one scheduling obligation, or nothing when it already exists."""
    return await conn.fetchval(
        """
        INSERT INTO agent_goal_wakeups
            (id, thread_id, goal_id, revision, trigger, trigger_ref, waiting_job_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (thread_id, trigger, trigger_ref) WHERE state = 'pending'
        DO NOTHING
        RETURNING id
        """,
        uuid.uuid4().hex,
        thread_id,
        goal_id,
        revision,
        trigger,
        trigger_ref,
        waiting_job_id,
    )
