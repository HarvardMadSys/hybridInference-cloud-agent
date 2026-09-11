"""Job row projections, and the statements built from them.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

# A job's own columns. What a job shares with every other turn of its
# conversation — repo, setup script, execution profile, tool servers — lives on
# the thread, so every read of a job joins ``agent_threads t`` and reports the
# thread's values under the job's keys.
JOB_OWN_COLUMNS = (
    "id, thread_id, parent_job_id, turn_no, native_turn_no, user_id, base_sha, task_prompt, "
    "runtime, model, reasoning_effort, state, "
    "cancel_requested, terminal_resume_pending, current_attempt_id, "
    "published_pr_url, published_commit_sha, detail, "
    "metadata, fork_source_job_id, "
    "created_at, updated_at"
)

THREAD_CONFIG_COLUMNS = "t.repo, t.setup_script, t.execution_profile, t.mcp_servers"

# Probe the unfinished-turn index before the latest-turn index: a cancelled
# queued follow-up must not hide work that is still running earlier in the chat.
CONVERSATION_STATE_SQL = """
COALESCE(
    (SELECT active.state FROM agent_jobs active
     WHERE active.thread_id = j.thread_id AND active.user_id = j.user_id
       AND active.state IN ('queued', 'running')
     ORDER BY CASE WHEN active.state = 'running' THEN 0 ELSE 1 END, active.turn_no DESC
     LIMIT 1),
    (SELECT CASE WHEN latest.state = 'failed' THEN 'failed' ELSE 'idle' END
     FROM agent_jobs latest
     WHERE latest.thread_id = j.thread_id AND latest.user_id = j.user_id
     ORDER BY latest.turn_no DESC LIMIT 1)
)"""

QUALIFIED_JOB_COLUMNS = (
    ", ".join(f"j.{column.strip()}" for column in JOB_OWN_COLUMNS.split(","))
    + ", "
    + THREAD_CONFIG_COLUMNS
    + f", {CONVERSATION_STATE_SQL} AS conversation_state"
)

JOB_SELECT = (
    f"SELECT {QUALIFIED_JOB_COLUMNS} FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id"
)

# An idle native process and its HOT credential lease outlive the completed
# turn. Removing a chat from owner history must not tear down that process or
# its shared workspace. Actual work and unfinished publication remain visible
# until they settle; the retained rows keep lineage and billing attributable.
SESSION_DELETE_BLOCK_SQL = """
CASE
    WHEN t.archived_at IS NULL THEN 'not_archived'
    WHEN EXISTS (
        SELECT 1 FROM agent_jobs busy_job
        WHERE busy_job.thread_id = t.session_id
          AND busy_job.state NOT IN ('succeeded', 'failed', 'cancelled')
    ) OR EXISTS (
        SELECT 1 FROM agent_attempts a
        JOIN agent_jobs busy_job ON busy_job.id = a.job_id
        WHERE busy_job.thread_id = t.session_id
          AND a.status = 'running' AND a.lease_expires_at > NOW()
    ) OR EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships m
        WHERE m.thread_id = t.session_id
          AND (m.active_attempt_id IS NOT NULL OR m.resident_state IN ('starting', 'restarting'))
    ) THEN 'session_active'
    WHEN EXISTS (
        SELECT 1 FROM agent_session_forks f
        WHERE (f.source_thread_id = t.session_id OR f.target_thread_id = t.session_id)
          AND f.state IN ('preparing', 'materialized')
    ) OR EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships m
        WHERE m.thread_id = t.session_id AND m.fork_operation_id IS NOT NULL
    ) THEN 'fork_in_progress'
    WHEN EXISTS (
        SELECT 1 FROM agent_sandbox_snapshots snapshot
        JOIN agent_snapshot_jobs included ON included.snapshot_id = snapshot.id
        JOIN agent_jobs included_job ON included_job.id = included.job_id
        WHERE included_job.thread_id = t.session_id
          AND snapshot.state IN ('pending', 'capturing', 'captured', 'publishing')
    ) THEN 'publication_pending'
    ELSE NULL
END
"""


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
