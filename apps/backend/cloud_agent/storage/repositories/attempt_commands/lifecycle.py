"""A claimed attempt's heartbeat, transitions and cancellation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.execution import CANCELLED, QUEUED, RUNNING, TERMINAL_STATES
from cloud_agent.domain.pending_messages import QUEUE_PAUSED_STOPPED
from cloud_agent.storage._sql.fences import leased_attempt_sql
from cloud_agent.storage._sql.goals import arm_finish_wakeup, goal_after_terminal
from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST, release_host_slot
from cloud_agent.storage._sql.jobs import (
    fail_discontinuous_follow_ups,
    fail_unrunnable_follow_ups,
    lock_conversation_by_job,
)
from cloud_agent.storage._sql.pending_messages import PAUSE_QUEUE_SQL
from cloud_agent.storage._sql.sandbox import release_restore
from cloud_agent.storage._sql.sandbox_transitions import (
    TRANSITION_GROUP_SQL,
    TRANSITION_MEMBERS_SQL,
)
from cloud_agent.storage.repositories.attempt_commands._common import AttemptCommandsBase

if TYPE_CHECKING:
    from cloud_agent.domain.goals import GoalReportEnvelope


class LifecycleMixin(AttemptCommandsBase):
    """Heartbeat, transition and cancel attempts."""

    async def heartbeat(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        lease_ttl_seconds: float,
    ) -> dict[str, Any]:
        """Extend a live lease and report cancellation, fenced by generation.

        Returns ``{"ok": False}`` when the attempt is no longer the owner
        (superseded, finished, or generation mismatch) — the worker must stop
        immediately. On success also returns ``cancel_requested`` so workers
        learn about cancellation without extra polling.

        The machine the attempt runs on must also still be enrolled. The
        worker token proves only the fence, and outlives the host credential
        it was issued under; without this predicate a removed host could keep
        its leases — and with them everything they renew — alive forever.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_live_attempt(conn, attempt_id, lease_generation):
                return {"ok": False}
            job_id = await conn.fetchval(
                f"""
                UPDATE agent_attempts a
                SET lease_expires_at = clock_timestamp() + make_interval(secs => $3)
                WHERE {leased_attempt_sql("$1", "$2")}
                  AND {LIVE_ATTEMPT_HOST}
                RETURNING a.job_id
                """,
                attempt_id,
                lease_generation,
                lease_ttl_seconds,
            )
            if job_id is None:
                return {"ok": False}
            row = await conn.fetchrow(
                "SELECT state, cancel_requested FROM agent_jobs WHERE id = $1", job_id
            )
        return {
            "ok": True,
            "job_id": job_id,
            "state": row["state"],
            "cancel_requested": row["cancel_requested"],
        }

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
        goal_envelope: GoalReportEnvelope | None = None,
    ) -> bool:
        """Move a job between states, fenced by the attempt's live lease.

        Terminal transitions also close the attempt (``finished``). Returns
        False when the caller lost the lease or the job is not in
        ``from_states`` — the caller must stop.

        ``goal_envelope`` is how the turn's final message read, and it is
        stored with the terminal state *and* the scheduling obligation it
        implies, in one transaction: either the turn ended and the goal was told
        to look at itself, or neither happened and the finish is retried.

        ``base_sha`` fills in the commit the worker resolved, and only when the
        job does not already have one: an owner who pinned a commit must get a
        patch against *that* commit, so a worker may report the base it used
        but never overwrite the base it was given.

        Raises ``ValueError`` for a ``succeeded`` transition on a turn that
        never settled its native context: the schema refuses the row, and the
        caller — not this method — is what has to change.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Lock the attempt before its job, matching release_claim and the
            # reaper. Without an explicit first lock, the job UPDATE below and
            # a concurrent abort can each hold the row the other needs.
            if not await self._lock_live_attempt(conn, attempt_id, lease_generation):
                return False
            # Then the conversation, before any job row: the terminal sweep below
            # can pause this conversation's goal, and that write has to be taken
            # on the way *down* the order rather than reached for at the end.
            await lock_conversation_by_job(conn, job_id=job_id)
            # A success carries its native turn ordinal. The runner settles the
            # shared workspace before it reports success and settlement assigns
            # the ordinal; the schema refuses a succeeded row without one, and
            # nothing here guesses it on the caller's behalf — a success that
            # skipped settlement is a protocol violation and fails loudly.
            try:
                updated = await self._transition_job(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                    from_states=from_states,
                    to_state=to_state,
                    detail=detail,
                    base_sha=base_sha,
                )
            except asyncpg.CheckViolationError as exc:
                if "native_turn" not in str(exc):
                    raise
                raise ValueError(
                    "a succeeded turn must settle its native context before it finishes"
                ) from exc
            if updated is None:
                return False
            if goal_envelope is not None:
                # The report and the outcome are one commit. Written here rather
                # than after the transition because a terminal turn whose report
                # landed later would be a turn the decision judged blind.
                await conn.execute(
                    """
                    UPDATE agent_jobs
                    SET goal_report_parse = $2, goal_report_outcome = $3,
                        goal_report_reason = $4, goal_report_detail = $5,
                        goal_report_evidence = $6, goal_report_digest = $7,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    job_id,
                    goal_envelope.parse_result,
                    goal_envelope.outcome,
                    goal_envelope.reason,
                    goal_envelope.detail,
                    # Serialized here rather than handed over as a list: this
                    # pool has no JSONB codec registered, so the driver wants
                    # the text a JSON column is written from.
                    json.dumps(goal_envelope.evidence)
                    if goal_envelope.evidence is not None
                    else None,
                    goal_envelope.source_digest,
                )
            if to_state in TERMINAL_STATES:
                # A turn that ends while its attempt was still recreating the
                # group admitted nothing. Hand the group back to its checkpoint
                # first, so the backstop below does not read the missing
                # container as lost context. A turn failed before its host
                # reserved the group -- the hand-over refused it, or the host
                # could not start it -- read no checkpoint and spends none of
                # the group's attempts.
                if (
                    await release_restore(
                        conn, attempt_id, detail or "turn ended during sandbox restore"
                    )
                    == "lost"
                    and updated["thread_id"] is not None
                ):
                    await fail_unrunnable_follow_ups(
                        conn,
                        thread_id=updated["thread_id"],
                        detail="not run because the sandbox could not be restored",
                    )
                sandbox_transition = None
                # Close any retained-sandbox turn boundary in the same
                # transaction as the job outcome.  A successful turn has
                # already persisted ``last_settled_job_id`` and only clears
                # its admission receipt.  Finishing before that boundary is
                # either an admitted/ambiguous turn (LOST) or a pre-admission
                # failure (STOPPING, safe to contain without replaying stdin).
                #
                # This is also the crash backstop for the runner's finally:
                # a terminal job must never leave ``active_attempt_id`` stuck
                # on an attempt the reaper will no longer visit.
                if updated["thread_id"] is not None:
                    sandbox_transition = await conn.fetchrow(
                        TRANSITION_MEMBERS_SQL,
                        job_id,
                        attempt_id,
                        updated["thread_id"],
                        detail,
                    )
                    if sandbox_transition is not None:
                        await conn.execute(
                            TRANSITION_GROUP_SQL,
                            sandbox_transition["sandbox_id"],
                            bool(sandbox_transition["settled"]),
                            sandbox_transition["context_status"] == "lost",
                            updated["thread_id"],
                        )
                    if (
                        sandbox_transition is not None
                        and sandbox_transition["context_status"] == "lost"
                    ):
                        await conn.execute(
                            "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                            attempt_id,
                        )
                        await fail_unrunnable_follow_ups(
                            conn,
                            thread_id=updated["thread_id"],
                        )
                finished = await conn.fetchrow(
                    "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                    "WHERE id = $1 AND status = 'running' RETURNING host_id",
                    attempt_id,
                )
                # Only when this call is the one that closed the attempt. The
                # job-level fence above already makes a second terminal
                # transition impossible, but the slot must not depend on that
                # being true two layers away.
                if finished is not None:
                    await release_host_slot(conn, finished["host_id"])
                await conn.execute(
                    "UPDATE agent_jobs SET terminal_resume_pending = FALSE WHERE id = $1",
                    job_id,
                )
                # Native-session continuity is ready at settlement, so the
                # next message in this shared workspace need not wait.
                if not (
                    sandbox_transition is not None
                    and sandbox_transition["context_status"] == "lost"
                ):
                    await fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
                await arm_finish_wakeup(conn, updated, job_id=job_id)
        return True

    @staticmethod
    async def _transition_job(
        conn: Any,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None,
        base_sha: str | None,
    ) -> Any:
        """The fenced job UPDATE behind :meth:`transition`; ``None`` when fenced out."""
        return await conn.fetchrow(
            f"""
            UPDATE agent_jobs j
            SET state = $4,
                detail = COALESCE($5, j.detail),
                base_sha = COALESCE(j.base_sha, $7),
                updated_at = NOW()
            WHERE j.id = $1
              AND j.state = ANY($3::text[])
              AND j.current_attempt_id = $2
              AND EXISTS (
                    SELECT 1 FROM agent_attempts a
                    WHERE {leased_attempt_sql("$2", "$6")}
              )
            RETURNING j.id, j.thread_id, j.goal_id, j.goal_revision,
                      j.turn_source, j.parent_job_id
            """,
            job_id,
            attempt_id,
            list(from_states),
            to_state,
            detail,
            lease_generation,
            base_sha,
        )

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Request cancellation; returns the job's resulting state.

        Queued jobs cancel immediately. Running jobs get the flag
        set — the worker observes it on its next heartbeat and performs the
        fenced transition. Terminal jobs are a no-op. ``user_id`` scopes the
        cancel to the owner. Returns ``None`` for an unknown job.

        Cancelling a queued or running turn also pauses the conversation's
        pending messages, if it has any, with the reason ``stopped``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Conversation first: cancelling a queued automatic round pauses its
            # goal, and that write lands on ``agent_threads`` — which the pause
            # and follow-up paths hold while reaching for this very job row.
            await lock_conversation_by_job(conn, job_id=job_id)
            row = await conn.fetchrow(
                "SELECT state, thread_id FROM agent_jobs WHERE id = $1 "
                "AND ($2::text IS NULL OR user_id = $2) FOR UPDATE",
                job_id,
                user_id,
            )
            if row is None:
                return None
            state = row["state"]
            if state in (QUEUED, RUNNING):
                # Stop means the owner's waiting messages wait too: the turn
                # after this one is theirs to start, not the queue's. Written in
                # this transaction, under the conversation's lock taken above,
                # so no settlement can slip the next message in between the
                # cancel and the pause.
                await conn.execute(PAUSE_QUEUE_SQL, [row["thread_id"]], QUEUE_PAUSED_STOPPED)
            if state == QUEUED:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                    "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                    job_id,
                )
                # A queued automatic round that has just been cancelled is a
                # goal that can no longer continue itself. Cancel the round and
                # the goal stops in the same transaction: there is no window in
                # which a cancelled round leaves an active goal with nothing to
                # run it.
                await goal_after_terminal(
                    conn,
                    job_id=job_id,
                    thread_id=row["thread_id"],
                    state=CANCELLED,
                    detail="the automatic round was cancelled before it ran",
                )
                await fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
                return CANCELLED
            if state == RUNNING:
                # Deliberately no goal write here: this only asks the running
                # turn to stop, and it stops by reaching a terminal state
                # through ``transition``, which is where every other outcome is
                # decided. Pausing now would race the decision that is about to
                # be made about this same turn.
                await conn.execute(
                    "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                    "WHERE id = $1",
                    job_id,
                )
            return state
