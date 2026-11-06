"""Reaping attempts whose lease lapsed."""

from __future__ import annotations

from typing import Any

import asyncpg

from cloud_agent.domain.execution import (
    ATTEMPT_ABORTED,
    CANCELLED,
    EVENT_ATTEMPT_SUPERSEDED,
    FAILED,
    QUEUED,
    TERMINAL_STATES,
)
from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.goals import goal_after_terminal
from cloud_agent.storage._sql.hosts import release_host_slot
from cloud_agent.storage._sql.jobs import fail_discontinuous_follow_ups, fail_unrunnable_follow_ups
from cloud_agent.storage._sql.sandbox import release_restore
from cloud_agent.storage._sql.sandbox_transitions import (
    REAP_ATTEMPT_GROUP_SQL,
    REAP_ATTEMPT_MEMBERS_2_SQL,
    REAP_ATTEMPT_MEMBERS_3_SQL,
    REAP_ATTEMPT_MEMBERS_SQL,
)
from cloud_agent.storage.repositories.attempt_commands._common import AttemptCommandsBase, logger
from cloud_agent_shared.sandbox_restore import MAX_RESTORE_ATTEMPTS


class ReaperMixin(AttemptCommandsBase):
    """Close attempts whose lease expired."""

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs.

        For each expired ``running`` attempt: mark it ``superseded``, append an
        ``attempt_superseded`` control event, then decide the job's fate —
        requeue while attempts remain, ``cancelled`` if cancellation was
        pending, ``failed`` once ``max_attempts`` is exhausted. Returns a
        summary per reaped attempt.

        Each attempt is reaped in a transaction of its own. One the database
        refuses -- a deadlock against a live writer -- stays expired for the
        next pass, instead of taking back every attempt this pass closed.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn:
            expired = await conn.fetch(
                "SELECT id FROM agent_attempts "
                "WHERE status = 'running' AND lease_expires_at < clock_timestamp() ORDER BY id"
            )
            for candidate in expired:
                try:
                    async with conn.transaction():
                        action = await self._reap_attempt(
                            conn, attempt_id=candidate["id"], max_attempts=max_attempts
                        )
                except asyncpg.PostgresError:
                    logger.warning(
                        "agent_attempt_reap_failed",
                        exc_info=True,
                        extra={
                            "event": "agent_attempt_reap_failed",
                            "attempt_id": candidate["id"],
                        },
                    )
                    continue
                if action is not None:
                    actions.append(action)
        return actions

    async def _reap_attempt(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        self, conn: Any, *, attempt_id: int, max_attempts: int
    ) -> dict[str, Any] | None:
        """Reap one expired attempt inside the caller's transaction.

        ``None`` when it is no longer expired and running, or a writer holds
        its attempt, job or conversation: the next pass looks again.
        """
        row = await conn.fetchrow(
            """
            SELECT a.id AS attempt_id, a.job_id, j.thread_id, a.attempt_no, a.host_id,
                   j.state, j.cancel_requested, j.current_attempt_id,
                   member.thread_id AS sandbox_thread_id,
                   member.sandbox_id,
                   member.active_attempt_id AS sandbox_active_attempt_id,
                   member.admitted_job_id AS sandbox_admitted_job_id,
                   member.last_settled_job_id AS sandbox_last_settled_job_id,
                   member.residency_generation
            FROM agent_attempts a
            JOIN agent_jobs j ON j.id = a.job_id
            JOIN agent_threads t ON t.id = j.thread_id
            LEFT JOIN (
                agent_thread_sandbox_memberships member
                JOIN agent_sandboxes sandbox
                  ON sandbox.id = member.sandbox_id
            ) ON member.thread_id = j.thread_id
            WHERE a.id = $1 AND a.status = 'running' AND a.lease_expires_at < clock_timestamp()
            -- The conversation is locked here, with the attempt and the
            -- job, rather than reached for later when this pass pauses a
            -- goal: taken in one statement it is covered by SKIP LOCKED,
            -- so a conversation somebody is already pausing is left for
            -- the next pass instead of waiting on a row that is waiting
            -- on this one.
            FOR UPDATE OF a, j, t SKIP LOCKED
            """,
            attempt_id,
        )
        if row is None:
            return None
        job_id = row["job_id"]
        await conn.execute(
            "UPDATE agent_attempts SET status = 'superseded', finished_at = NOW() WHERE id = $1",
            attempt_id,
        )
        await insert_event(
            conn,
            job_id=job_id,
            attempt_id=attempt_id,
            event_type=EVENT_ATTEMPT_SUPERSEDED,
            payload={"attempt_no": row["attempt_no"], "reason": "lease_expired"},
        )
        sandbox_thread_id = row["sandbox_thread_id"]
        sandbox_active_attempt_id = row["sandbox_active_attempt_id"]
        sandbox_admitted_job_id = row["sandbox_admitted_job_id"]
        sandbox_last_settled_job_id = row["sandbox_last_settled_job_id"]
        settled_retained_turn = sandbox_last_settled_job_id == job_id
        admitted_ambiguously = sandbox_admitted_job_id == job_id and not settled_retained_turn
        owns_retained_activation = sandbox_active_attempt_id == attempt_id
        retained_activation_expired = (
            admitted_ambiguously or owns_retained_activation
        ) and not settled_retained_turn
        settled_finalization_expired = settled_retained_turn and sandbox_admitted_job_id == job_id
        # An attempt that died while recreating a cold group from its
        # checkpoint admitted nothing: admission waits for the recreated
        # container. The group goes back to its checkpoint and the turn back
        # to the queue, until the restore attempts run out. One that died
        # before its host reserved the group never began the restore, and
        # spends none of them.
        restored_state = await release_restore(conn, attempt_id, "sandbox restore lease expired")
        restore_exhausted = restored_state == "lost"
        if restored_state is not None:
            retained_activation_expired = False
            if restore_exhausted:
                await fail_unrunnable_follow_ups(conn, thread_id=sandbox_thread_id)
        cleanup_residency: dict[str, Any] | None = None
        if retained_activation_expired:
            # The admission receipt identifies a definitely ambiguous
            # prompt. Even before admission, an expired runner may
            # have created a container without returning its identity
            # to the control plane. The preview therefore fails all
            # expired retained activations closed instead of guessing
            # that a retry is safe; ordinary CLI/process replacement
            # at a clean turn boundary remains supported.
            lost_member = await conn.fetchrow(
                REAP_ATTEMPT_MEMBERS_SQL,
                job_id,
                attempt_id,
                sandbox_thread_id,
                row["sandbox_id"],
            )
            if lost_member is not None:
                await conn.execute(
                    REAP_ATTEMPT_GROUP_SQL,
                    row["sandbox_id"],
                )
                residency_generation = int(lost_member["residency_generation"] or 0)
                if residency_generation > 0:
                    released = await conn.fetchrow(
                        REAP_ATTEMPT_MEMBERS_2_SQL,
                        sandbox_thread_id,
                        residency_generation,
                    )
                    if released is not None:
                        cleanup_residency = dict(released)
                await conn.execute(
                    "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                    attempt_id,
                )
                await fail_unrunnable_follow_ups(
                    conn,
                    thread_id=sandbox_thread_id,
                )
            else:
                # The read above does not lock the membership: a writer that
                # does not take the attempt can still release the activation
                # between that read and this write. Nothing was marked lost,
                # so nothing below treats the context as lost, and the turn
                # ends as any expired turn does.
                retained_activation_expired = False
        elif settled_finalization_expired:
            # The native context boundary is durable; only the final
            # job transition was lost. Never replay this prompt and
            # never relabel its usable context LOST. Close admission,
            # preserve the settled idle deadline, and fail the job for
            # explicit operator/user review.
            await conn.execute(
                REAP_ATTEMPT_MEMBERS_3_SQL,
                job_id,
                attempt_id,
                sandbox_thread_id,
            )
        if row["current_attempt_id"] != attempt_id or row["state"] in TERMINAL_STATES:
            # Stale attempt of an already-moved-on job: closing it is enough.
            action = "closed_stale"
        elif retained_activation_expired:
            action = CANCELLED if row["cancel_requested"] else FAILED
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = $2,
                    detail = $3,
                    terminal_resume_pending = TRUE,
                    updated_at = NOW()
                WHERE id = $1
                """,
                job_id,
                action,
                (
                    "cancelled after an indeterminate retained Agent CLI turn"
                    if row["cancel_requested"]
                    else (
                        "retained Agent CLI turn became indeterminate after prompt admission"
                        if admitted_ambiguously
                        else "retained sandbox activation lease expired; context marked lost"
                    )
                ),
            )
        elif settled_finalization_expired:
            action = CANCELLED if row["cancel_requested"] else FAILED
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = $2,
                    detail = $3,
                    terminal_resume_pending = TRUE,
                    updated_at = NOW()
                WHERE id = $1
                """,
                job_id,
                action,
                (
                    "cancelled after the Agent CLI turn settled"
                    if row["cancel_requested"]
                    else (
                        "Agent CLI context settled, but job finalization expired; "
                        "the prompt was not replayed"
                    )
                ),
            )
        elif restore_exhausted and not row["cancel_requested"]:
            action = FAILED
            await conn.execute(
                "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                job_id,
                f"the sandbox could not be restored after {MAX_RESTORE_ATTEMPTS} attempts",
            )
        elif row["cancel_requested"]:
            action = CANCELLED
            await conn.execute(
                "UPDATE agent_jobs SET state = 'cancelled', "
                "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                job_id,
            )
        elif await self._attempts_spent(conn, job_id) >= max_attempts:
            action = FAILED
            await conn.execute(
                "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                job_id,
                f"exhausted {max_attempts} attempts (lease expired)",
            )
        else:
            action = QUEUED
            # The next attempt answers for itself. A report the
            # abandoned attempt left behind is not evidence about the
            # retry: inheriting a `complete` from a turn that never
            # finished would complete the goal on the strength of a
            # claim whose work was thrown away.
            await conn.execute(
                "UPDATE agent_jobs SET state = 'queued', "
                "goal_report_parse = NULL, goal_report_outcome = NULL, "
                "goal_report_reason = NULL, goal_report_detail = NULL, "
                "goal_report_evidence = NULL, "
                "goal_report_digest = NULL, updated_at = NOW() "
                "WHERE id = $1",
                job_id,
            )
        if action in TERMINAL_STATES:
            # Every terminal reaper outcome is a turn that did not end
            # workably from the goal's point of view. The ordinary
            # finish path arms the goal's next step in
            # ``arm_finish_wakeup``; a reaped turn never reaches it,
            # so it is decided here — and the only safe decision is
            # to stop and wait for the owner.
            await goal_after_terminal(conn, job_id=job_id, thread_id=row["thread_id"], state=action)
        if action in TERMINAL_STATES and not retained_activation_expired:
            # A terminal reaper outcome is semantically the same turn
            # boundary as ``transition``. Keep the linear chat moving
            # when the context is usable; the LOST branch above has
            # already failed every unrunnable descendant instead.
            await fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
        # The slot last, after the membership and the group: claiming or
        # finishing a sibling on this host takes the group before the host
        # row. The SELECT took this row ``FOR UPDATE`` while it was still
        # ``running``, so this runs once per attempt however many reaper
        # passes overlap — which is what makes the slot release safe here
        # without a second predicate.
        await release_host_slot(conn, row["host_id"])
        action_result: dict[str, Any] = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "action": action,
        }
        if cleanup_residency is not None:
            action_result["cleanup_residency"] = cleanup_residency
        return action_result

    @staticmethod
    async def _attempts_spent(conn: Any, job_id: str) -> int:
        """Count attempts that actually got their turn.

        The retry budget used to be read off ``attempt_no``, which numbers
        every claim including ones the platform abandoned before the agent
        started. Counting instead means an aborted claim is free: numbers still
        advance (they are referenced by events, and gaps are informative), but
        only attempts that ran spend the budget.
        """
        return await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND status <> $2",
            job_id,
            ATTEMPT_ABORTED,
        )
