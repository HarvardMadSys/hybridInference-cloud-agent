"""Follow-up sequencing, advanced or failed with its parent's state."""

from __future__ import annotations

from typing import Any


async def fail_unrunnable_follow_ups(
    conn: Any,
    *,
    thread_id: str,
    detail: str = "not run because an earlier turn lost the retained sandbox context",
) -> None:
    """Give queued descendants a terminal answer when continuity is LOST.

    A retained chat cannot be claimed again after its context becomes
    indeterminate. Leaving already-submitted turns in ``queued`` would
    therefore create a permanent queue entry rather than
    a recoverable retry. Every LOST transition calls this helper in the
    same transaction that fences the sandbox row.
    """
    await conn.execute(
        """
        UPDATE agent_jobs
        SET state = 'failed', detail = $2, updated_at = NOW()
        WHERE thread_id = $1 AND state = 'queued'
        """,
        thread_id,
        detail[:2000],
    )


async def fail_discontinuous_follow_ups(
    conn: Any,
    *,
    parent_job_id: str | None = None,
) -> None:
    """Fail queued descendants that cannot preserve native continuity.

    A successful parent requires a usable shared membership. The whole
    pending tail fails if that membership is missing or lost. Terminal
    writers scope this cleanup to the parent they just changed; the
    periodic sweep handles legacy rows. Scheduling itself only checks
    eligibility and never needs to promote a queued turn.
    """
    await conn.execute(
        """
        WITH invalid_threads AS (
            SELECT DISTINCT child.thread_id
            FROM agent_jobs child
            JOIN agent_jobs parent ON parent.id = child.parent_job_id
            WHERE child.state = 'queued'
              AND parent.state = 'succeeded'
              AND ($1::text IS NULL OR parent.id = $1)
              AND NOT EXISTS (
                    SELECT 1
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_sandboxes g ON g.id = m.sandbox_id
                    WHERE m.thread_id = child.thread_id
                      AND m.context_status <> 'lost'
                      AND m.native_state <> 'lost'
                      AND g.state NOT IN ('stopped_dirty', 'lost')
              )
        )
        UPDATE agent_jobs pending
        SET state = 'failed',
            detail = 'not run because its parent predates retained-session continuity',
            updated_at = NOW()
        WHERE pending.thread_id IN (SELECT thread_id FROM invalid_threads)
          AND pending.state = 'queued'
        """,
        parent_job_id,
    )
