"""Job row projections, and the statements built from them.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.goals import PAUSE_GOALS_SQL

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
    "metadata, skills, attachments, fork_source_job_id, "
    "turn_source, goal_round, goal_round_kind, goal_revision, goal_id, "
    "goal_report_parse, goal_report_outcome, goal_report_reason, goal_report_detail, "
    "goal_repair_round, "
    "input_seq_at_create, created_at, updated_at, revision"
)

# What a conversation shares with every one of its turns: where it runs, and —
# since the goal is conversation-level and single-valued — what it is working
# toward. The counters travel with the objective because a response that showed
# one without the other would report progress that is not the goal's.
#
# The thread's ``goal_revision`` is aliased. ``agent_jobs`` has one too — the
# revision *this turn* was created for — and a row carrying two columns of the
# same name keeps only the later one, which would quietly make every job read
# report the conversation's revision as its own snapshot.
THREAD_CONFIG_COLUMNS = (
    "t.repo, t.setup_script, t.execution_profile, t.plugins, "
    "t.goal_id, t.goal_objective, t.goal_state, t.goal_pause_reason, "
    "t.goal_pause_detail, t.goal_revision AS thread_goal_revision, "
    "t.goal_max_rounds, t.goal_completion_basis, t.goal_completed_by_job_id, "
    "t.goal_created_at, t.goal_updated_at, "
    # The conversation's input counter, carried on every turn so that "has the
    # owner spoken since this turn was created?" is answerable from one read.
    "t.user_input_seq AS thread_user_input_seq, "
    # Aliased for the same reason as the goal revision: the job has its own.
    # Owners' tabs order pushed titles against the thread's counter.
    "t.revision AS thread_revision"
)

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

MCP_BINDING_COLUMNS = "COALESCE(b.mcp_bindings, '[]'::jsonb) AS mcp_bindings"

MCP_BINDING_JOIN = (
    "LEFT JOIN LATERAL ("
    "SELECT jsonb_agg(jsonb_build_object("
    "'connection_id', tb.connection_id, 'alias', tb.alias, 'label', c.label"
    ") ORDER BY tb.alias) AS mcp_bindings "
    "FROM agent_mcp_thread_bindings tb "
    "JOIN agent_mcp_connections c ON c.id = tb.connection_id "
    "WHERE tb.thread_id = t.id"
    ") b ON TRUE"
)

JOB_SELECT = (
    f"SELECT {QUALIFIED_JOB_COLUMNS}, {MCP_BINDING_COLUMNS} "
    f"FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id {MCP_BINDING_JOIN}"
)


# Owner polling reads need identities, not up to 128 KiB of instructions per turn.
# Execution, inheritance and fork keep JOB_SELECT's complete immutable snapshot.
SKILL_SUMMARY_SQL = """CASE WHEN j.skills IS NULL THEN NULL ELSE COALESCE(
    (SELECT jsonb_agg(item - 'instructions' ORDER BY ordinal)
     FROM jsonb_array_elements(j.skills) WITH ORDINALITY AS entries(item, ordinal)),
    '[]'::jsonb) END AS skills"""
QUALIFIED_JOB_SUMMARY_COLUMNS = QUALIFIED_JOB_COLUMNS.replace("j.skills", SKILL_SUMMARY_SQL)
JOB_SUMMARY_SELECT = JOB_SELECT.replace(QUALIFIED_JOB_COLUMNS, QUALIFIED_JOB_SUMMARY_COLUMNS)


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


#: Take the conversation's row before any of its jobs'.
#:
#: Every terminal path that can pause a goal writes ``agent_threads`` while it
#: already holds ``agent_jobs`` rows, and the writers on the other side --
#: ``pause_goal``, ``clear_goal``, ``set_goal`` and ``_create_follow_up`` --
#: take the conversation first and then reach those same job rows. Two orders
#: over the same two rows is a cycle, and it is not theoretical: cancelling a
#: queued automatic round while its goal is being paused reproduces
#: ``deadlock detected`` (40P01) against Postgres.
#:
#: The order is **attempt -> conversation -> job**, and the attempt comes first
#: because ``append_event`` was already taking it before the conversation --
#: every assistant message bumps ``agent_threads.updated_at`` under the attempt
#: lock that serializes ``seq``. Putting the conversation ahead of the attempt
#: instead trades one cycle for another, between a turn's events and the
#: transition that ends it; that was tried, reproduced, and is why this reads
#: the way it does.
_LOCK_CONVERSATION_BY_JOB = """
SELECT t.id FROM agent_threads t
JOIN agent_jobs j ON j.thread_id = t.id
WHERE j.id = $1
FOR UPDATE OF t
"""


async def lock_conversation_by_job(conn: Any, *, job_id: str) -> str | None:
    """Lock the conversation this job belongs to, or ``None`` if it has none.

    Called first in a transaction that may end a turn. ``None`` means the job
    is gone, which the caller's own read reports in its own words.
    """
    return await conn.fetchval(_LOCK_CONVERSATION_BY_JOB, job_id)


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
    invalid = await conn.fetch(
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
            RETURNING pending.thread_id
            """,
        parent_job_id,
    )
    # An automatic round that will now never run is the goal's answer about
    # this conversation's context, not the person's: the goal stops with it.
    # Scoped to the threads this statement just emptied, so an unrelated
    # failure elsewhere cannot pause a goal that is still moving.
    failed_threads = sorted({str(row["thread_id"]) for row in invalid})
    if failed_threads:
        await conn.execute(
            PAUSE_GOALS_SQL,
            failed_threads,
            "context_lost",
            "the conversation's session is gone; it cannot be resumed from a transcript",
        )
