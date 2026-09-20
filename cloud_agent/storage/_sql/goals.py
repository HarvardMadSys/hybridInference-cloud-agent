"""Goal SQL shared by the repositories and the paths that run inside someone else's transaction.

A turn can stop being workable somewhere that is not the ordinary finish
handler: it can be cancelled while it waits in the queue, its lease can expire
and be reaped, or its conversation's native context can be lost and take every
queued descendant with it. Each of those runs inside another writer's
transaction, so what they share lives here as one statement rather than as
three copies of the same rule.

Every one of them *pauses*, and every one of them names its reason and its
detail. None of them continues or completes: a goal whose turn did not end
workably stops and waits for its owner, which is the only answer that cannot
spend someone's money on a conversation that is broken.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

#: How many automatic rounds a goal has actually spent.
#:
#: **Derived, not stored.** A counter has to be incremented by whoever claims a
#: round, which meant ``claim_job`` writing ``agent_threads`` while already
#: holding an ``agent_jobs`` row lock — the reverse of this repository's
#: documented thread-before-job order, and therefore a deadlock against
#: ``pause_goal`` and the follow-up append. A counter also has to be fenced
#: against the revision it was granted for, and every fence that was slightly
#: wrong silently gave away a round.
#:
#: Counting instead makes the fence structural. A round belongs to the goal
#: identity it was created for (``j.goal_id = t.goal_id``), it costs nothing
#: until a model actually runs for it, and it costs once however many attempts
#: it takes (``EXISTS``). Both round kinds count — ``work`` and the bounded
#: ``repair`` — because the budget bounds *spend*, and a repair round is a model
#: round like any other.
#:
#: ``aborted`` is the one attempt status that is free: it means the claim was
#: handed back before the turn began. Every other status, ``superseded``
#: included, is a turn that ran and was paid for.
ROUNDS_USED_SQL = """(
        SELECT count(*) FROM agent_jobs work
        WHERE work.thread_id = t.id
          AND work.turn_source = 'goal'
          AND work.goal_id = t.goal_id
          AND work.goal_id IS NOT NULL
          AND EXISTS (
                SELECT 1 FROM agent_attempts a
                WHERE a.job_id = work.id AND a.status <> 'aborted'
          )
    ) AS goal_rounds_used"""

#: Repair rounds spent on the *current* objective, which is what bounds the
#: report-repair loop. Reset with the objective, exactly like the budget.
REPAIRS_USED_SQL = """(
        SELECT count(*) FROM agent_jobs repair
        WHERE repair.thread_id = t.id
          AND repair.turn_source = 'goal'
          AND repair.goal_id = t.goal_id
          AND repair.goal_id IS NOT NULL
          AND repair.goal_round_kind = 'repair'
    ) AS goal_repairs_used"""

#: Create, replace or edit a goal -- in one statement.
#:
#: One statement is not a style preference. PostgreSQL validates a CHECK
#: constraint per statement, not per transaction, so a write split across
#: several UPDATEs has to satisfy every constraint at each intermediate step,
#: and there is no ordering of "set the objective" / "set the identity" /
#: "set the state" that does.
#:
#: ``$2`` objective, ``$3`` max_rounds, ``$4`` the identity to mint, ``$5``
#: the state to leave the goal in, ``$6`` force a fresh identity (a goal that
#: had already completed, set again).
#:
#: A new objective mints a new ``goal_id`` *and* resets the repair counter: the
#: rounds spent on the old objective are not rounds spent on this one, and a
#: malformed report about the old objective has nothing to say about the new
#: one. The revision moves on every accepted write, including one that changed
#: only the cap, so a queued round carrying an older revision is discarded
#: rather than charged to the new one.
SET_GOAL_SQL = """
UPDATE agent_threads
SET goal_objective = $2,
    goal_max_rounds = $3,
    goal_id = CASE WHEN $6 OR goal_id IS NULL OR goal_objective IS DISTINCT FROM $2
                        OR goal_state = 'complete'
                   THEN $4 ELSE goal_id END,
    goal_repairs_used = CASE WHEN $6 OR goal_id IS NULL
                                  OR goal_objective IS DISTINCT FROM $2
                                  OR goal_state = 'complete'
                             THEN 0 ELSE goal_repairs_used END,
    goal_state = $5,
    goal_pause_reason = CASE WHEN $5 = 'active' THEN NULL ELSE COALESCE(goal_pause_reason, 'user') END,
    goal_pause_detail = CASE WHEN $5 = 'active' THEN NULL
                             ELSE COALESCE(goal_pause_detail, $7) END,
    goal_completion_basis = NULL,
    goal_completed_by_job_id = NULL,
    goal_created_at = COALESCE(goal_created_at, NOW()),
    goal_revision = goal_revision + 1,
    goal_updated_at = NOW(),
    updated_at = NOW()
WHERE id = $1
RETURNING id
"""

#: What a goal says when it is paused by an edit that did not resume it.
EDITED_WHILE_PAUSED_DETAIL = "the goal was edited while paused; resume it to continue working"

#: Cancel queued automatic rounds. They exist only because the goal asked for
#: them, and leaving one in the queue would run a turn in a conversation whose
#: goal just stopped, was replaced or was cleared. Takes a ``text[]`` of thread
#: ids, so one statement serves a single round, a whole conversation, or a page
#: of them.
CANCEL_QUEUED_GOAL_ROUNDS_SQL = """
UPDATE agent_jobs
SET state = 'cancelled', cancel_requested = TRUE,
    terminal_resume_pending = TRUE, updated_at = NOW()
WHERE thread_id = ANY($1::text[]) AND state = 'queued' AND turn_source = 'goal'
"""

#: Pause an active goal behind one thread, in the caller's transaction.
#: A goal that is not active is left exactly as it is — a paused goal stays
#: paused for the reason it was paused, and a complete one is not un-completed
#: by a late reaping pass.
#:
#: Used by :func:`goal_after_terminal`. The owner-facing pause is the goal
#: repository's own ``pause_goal``, which is revision-fenced; this one
#: deliberately is not, because a reaper has no revision to offer and a
#: revision it invented would be a fence it could not check.
PAUSE_GOAL_SQL = """
UPDATE agent_threads
SET goal_state = 'paused', goal_pause_reason = $2,
    goal_pause_detail = COALESCE($3, goal_pause_detail),
    goal_completion_basis = NULL, goal_completed_by_job_id = NULL,
    goal_revision = goal_revision + 1,
    goal_updated_at = NOW(), updated_at = NOW()
WHERE id = $1 AND goal_state = 'active'
"""

#: The same rule for a caller that has more than one thread at a time: the
#: archive page, and the sweep that fails a queued continuation when its
#: conversation's native context is gone. ``$2`` reason, ``$3`` detail.
PAUSE_GOALS_SQL = """
UPDATE agent_threads t
SET goal_state = 'paused', goal_pause_reason = $2,
    goal_pause_detail = COALESCE($3, goal_pause_detail),
    goal_completion_basis = NULL, goal_completed_by_job_id = NULL,
    goal_revision = t.goal_revision + 1,
    goal_updated_at = NOW(), updated_at = NOW()
WHERE t.id = ANY($1::text[]) AND t.goal_state = 'active'
RETURNING t.id
"""


#: One obligation for a conversation's active goal, whichever event owed it.
#: The CTE re-reads the thread row rather than trusting the caller's copy, and
#: the ``NOT EXISTS`` guard means a conversation that already has something
#: pending does not accumulate a second opinion about it.
_ARM_GOAL_WAKEUP_SQL = """
WITH locked AS (
    SELECT id, goal_id, goal_revision
    FROM agent_threads
    WHERE id = $2 AND goal_state = 'active' AND goal_id IS NOT NULL
    FOR UPDATE
)
INSERT INTO agent_goal_wakeups
    (id, thread_id, goal_id, revision, trigger, trigger_ref)
SELECT $1, locked.id, locked.goal_id, locked.goal_revision, $3, $4
FROM locked
WHERE NOT EXISTS (
    SELECT 1 FROM agent_goal_wakeups pending
    WHERE pending.thread_id = locked.id AND pending.state = 'pending'
)
ON CONFLICT (thread_id, trigger, trigger_ref) WHERE state = 'pending'
DO NOTHING
RETURNING id
"""


async def arm_goal_wakeup(
    conn: Any,
    *,
    thread_id: str,
    trigger: str,
    trigger_ref: str,
) -> str | None:
    """Record that this conversation's active goal has to be looked at.

    Returns the wakeup id, or ``None`` when there was nothing to arm — no
    active goal, or one that already has an obligation pending. Used by the
    paths that act on a goal without knowing its identity yet (a message
    arriving, a conversation losing its context), where re-reading the row
    under the lock is the only way to get the identity right.
    """
    return await conn.fetchval(
        _ARM_GOAL_WAKEUP_SQL,
        uuid.uuid4().hex,
        thread_id,
        trigger,
        trigger_ref,
    )


async def arm_finish_wakeup(conn: Any, turn: Any, *, job_id: str) -> None:
    """Record the scheduling obligation a finished turn implies, in its transaction.

    Two obligations, and both matter:

    * Every wakeup that was *parked behind* this turn is released. "A person is
      waiting, judge the goal when their turn ends" is only half a rule; this is
      the other half, and without it the goal would wait for a finish that has
      already happened.
    * A turn that was itself an automatic round makes a new decision possible, so
      a ``finish`` wakeup is armed for it. The event identity is the turn id, so
      a replayed finish finds the wakeup it already made.

    A person's turn arms no wakeup of its own: it is not an outcome the goal
    decided anything about, and the park release above already re-armed whatever
    was waiting on it.
    """
    thread_id = turn["thread_id"]
    if thread_id is None:
        return
    await conn.execute(
        """
        UPDATE agent_goal_wakeups
        SET waiting_job_id = NULL, lease_owner = NULL, lease_expires_at = NULL,
            updated_at = NOW()
        WHERE waiting_job_id = $1 AND state = 'pending'
        """,
        job_id,
    )
    if turn["turn_source"] != "goal" or turn["goal_id"] is None:
        return
    await conn.execute(
        """
        INSERT INTO agent_goal_wakeups
            (id, thread_id, goal_id, revision, trigger, trigger_ref)
        VALUES ($1, $2, $3, $4, 'finish', $5)
        ON CONFLICT (thread_id, trigger, trigger_ref) WHERE state = 'pending'
        DO NOTHING
        """,
        uuid.uuid4().hex,
        thread_id,
        turn["goal_id"],
        int(turn["goal_revision"] or 0),
        f"job:{job_id}",
    )


async def goal_after_terminal(
    conn: Any,
    *,
    job_id: str,
    thread_id: str,
    state: str,
    detail: str | None = None,
) -> bool:
    """Pause the goal behind a job this caller has just made terminal.

    ``thread_id`` is passed rather than looked up. Every caller reaches this
    holding the job's row lock, and a lookup here would take the thread's lock
    *after* it — the reverse of this repository's thread-before-job order, which
    deadlocks against pausing a goal or appending a follow-up.

    Only an automatic round pauses a goal this way. A person's own turn failing
    is reported to the person, and is not by itself a reason to stop their goal.
    """
    reason = "cancelled" if state == "cancelled" else "execution_failed"
    return bool(
        await conn.fetchval(
            """
            UPDATE agent_threads t
            SET goal_state = 'paused', goal_pause_reason = $3,
                goal_pause_detail = COALESCE($4, 'the automatic round that was '
                                               'running did not finish'),
                goal_completion_basis = NULL, goal_completed_by_job_id = NULL,
                goal_revision = t.goal_revision + 1,
                goal_updated_at = NOW(), updated_at = NOW()
            FROM agent_jobs j
            WHERE t.id = $1 AND j.id = $2 AND j.thread_id = t.id
              AND t.goal_state = 'active'
              AND j.turn_source = 'goal'
            RETURNING t.id
            """,
            thread_id,
            job_id,
            reason,
            detail,
        )
    )
