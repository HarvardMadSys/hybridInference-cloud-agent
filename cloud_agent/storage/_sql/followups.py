"""Follow-up sequencing, advanced or failed with its parent's state."""

from __future__ import annotations

from typing import Any

import cloud_agent.domain.execution as _execution


async def advance_waiting_follow_ups(
    conn: Any,
    *,
    parent_job_id: str | None = None,
) -> None:
    """Release only descendants whose retained continuity is provable.

    During the cutover, an old API can leave a waiting child behind an
    in-flight disposable parent. Once that parent becomes terminal, the
    child must not be promoted into a fresh retained sandbox and presented
    as continuous. Fail the whole pending tail first, then release only a
    child backed by a usable shared membership or by an explicitly
    post-cutover parent that failed before producing native context.
    """
    await conn.execute(
        """
        WITH invalid_threads AS (
            SELECT DISTINCT child.thread_id
            FROM agent_jobs child
            JOIN agent_jobs parent ON parent.id = child.parent_job_id
            WHERE child.state IN ('waiting', 'queued')
              AND parent.state = ANY($2::text[])
              AND ($1::text IS NULL OR parent.id = $1)
              AND NOT (
                    (
                        parent.state IN ('failed', 'cancelled')
                        AND parent.retained_followup_eligible IS TRUE
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM agent_thread_sandbox_memberships m
                        JOIN agent_sandboxes g ON g.id = m.sandbox_id
                        WHERE m.thread_id = child.thread_id
                          AND g.mode = 'shared_live'
                          AND m.context_status <> 'lost'
                          AND m.native_state <> 'lost'
                          AND g.state NOT IN ('stopped_dirty', 'lost')
                    )
              )
        )
        UPDATE agent_jobs pending
        SET state = 'failed',
            detail = 'not run because its parent predates retained-session continuity',
            updated_at = NOW()
        WHERE pending.thread_id IN (SELECT thread_id FROM invalid_threads)
          AND pending.state IN ('waiting', 'queued')
        """,
        parent_job_id,
        list(_execution.TERMINAL_STATES),
    )
    await conn.execute(
        """
        UPDATE agent_jobs child
        SET state = 'queued', updated_at = NOW()
        FROM agent_jobs parent
        WHERE child.state = 'waiting'
          AND child.parent_job_id = parent.id
          AND parent.state = ANY($2::text[])
          AND ($1::text IS NULL OR parent.id = $1)
          AND (
                (
                    parent.state IN ('failed', 'cancelled')
                    AND parent.retained_followup_eligible IS TRUE
                )
                OR EXISTS (
                    SELECT 1
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_sandboxes g ON g.id = m.sandbox_id
                    WHERE m.thread_id = child.thread_id
                      AND g.mode = 'shared_live'
                      AND m.context_status <> 'lost'
                      AND m.native_state <> 'lost'
                      AND g.state NOT IN ('stopped_dirty', 'lost')
                )
          )
        """,
        parent_job_id,
        list(_execution.TERMINAL_STATES),
    )


async def fail_unrunnable_follow_ups(
    conn: Any,
    *,
    thread_id: str,
    detail: str = "not run because an earlier turn lost the retained sandbox context",
) -> None:
    """Give queued descendants a terminal answer when continuity is LOST.

    A retained chat cannot be claimed again after its context becomes
    indeterminate. Leaving already-submitted turns in ``waiting`` or
    ``queued`` would therefore create a permanent queue entry rather than
    a recoverable retry. Every LOST transition calls this helper in the
    same transaction that fences the sandbox row.
    """
    await conn.execute(
        """
        UPDATE agent_jobs
        SET state = 'failed', detail = $2, updated_at = NOW()
        WHERE thread_id = $1 AND state IN ('waiting', 'queued')
        """,
        thread_id,
        detail[:2000],
    )
