"""Give a conversation a completion goal the platform judges after every turn.

Revision ID: 0019_conversation_goal
Revises: 0018_profiling_control

The goal is conversation-level and single-valued: ``agent_threads`` holds the
objective and the counters, and "no goal" is ``goal_objective IS NULL``. The
per-turn snapshot lives on ``agent_jobs`` (what this turn was told, what kind
of round it was, and what it reported) so that editing the objective never
rewrites history -- the same semantics ``agent_jobs.skills`` already has.

Nothing is backfilled: every existing row has no goal, which is exactly what
the shape constraint asserts. ``0016_job_attachments`` precedes it; the two are
independent and either order would do.
"""

from alembic import op

revision = "0019_conversation_goal"
down_revision = "0018_profiling_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the conversation goal, its counters, and the per-turn snapshot."""
    op.execute(
        """
        ALTER TABLE agent_threads
            ADD COLUMN goal_objective TEXT
                CHECK (goal_objective IS NULL
                       OR char_length(goal_objective) BETWEEN 1 AND 4000),
            ADD COLUMN goal_state TEXT
                CHECK (goal_state IS NULL
                       OR goal_state IN ('active', 'paused', 'blocked', 'complete')),
            ADD COLUMN goal_pause_reason TEXT
                CHECK (goal_pause_reason IS NULL
                       OR goal_pause_reason IN (
                           'user', 'fork', 'turn-failed', 'cancelled',
                           'context-lost', 'archived', 'stalled')),
            ADD COLUMN goal_blocked_code TEXT
                CHECK (goal_blocked_code IS NULL
                       OR goal_blocked_code IN ('round-limit', 'model-reported')),
            ADD COLUMN goal_blocked_message TEXT
                CHECK (goal_blocked_message IS NULL
                       OR char_length(goal_blocked_message) <= 2000),
            ADD COLUMN goal_revision INT NOT NULL DEFAULT 0,
            ADD COLUMN goal_max_rounds INT
                CHECK (goal_max_rounds IS NULL
                       OR goal_max_rounds BETWEEN 1 AND 50),
            -- Rounds spent are counted from the rounds that actually started,
            -- never stored: a stored counter has to be incremented by whoever
            -- claims a round, and that write lands on this table while the
            -- claimer already holds the job's row lock.
            ADD COLUMN goal_blocked_streak INT NOT NULL DEFAULT 0,
            ADD COLUMN goal_created_at TIMESTAMPTZ,
            ADD COLUMN goal_updated_at TIMESTAMPTZ,
            ADD CONSTRAINT ck_agent_threads_goal_shape CHECK (
                (goal_objective IS NULL) = (goal_state IS NULL)
                AND (goal_objective IS NULL) = (goal_max_rounds IS NULL)
            )
        """
    )
    op.execute(
        """
        ALTER TABLE agent_jobs
            ADD COLUMN turn_source TEXT NOT NULL DEFAULT 'human'
                CHECK (turn_source IN ('human', 'goal')),
            ADD COLUMN goal_round INT,
            ADD COLUMN goal_round_kind TEXT
                CHECK (goal_round_kind IS NULL
                       OR goal_round_kind IN ('work', 'verify')),
            ADD COLUMN goal_revision INT,
            ADD COLUMN goal_report TEXT
                CHECK (goal_report IS NULL
                       OR goal_report IN ('continue', 'complete', 'blocked')),
            ADD COLUMN goal_reason TEXT
                CHECK (goal_reason IS NULL OR char_length(goal_reason) <= 2000)
        """
    )
    # The claim path asks "is a goal round already waiting in this thread?" on
    # every finish. Partial, because only queued goal rounds are ever sought.
    op.execute(
        """
        CREATE INDEX idx_agent_jobs_thread_goal_queued
            ON agent_jobs (thread_id)
            WHERE state = 'queued' AND turn_source = 'goal'
        """
    )


def downgrade() -> None:
    """Remove the goal and every turn's snapshot of it."""
    op.execute("DROP INDEX idx_agent_jobs_thread_goal_queued")
    op.execute(
        """
        ALTER TABLE agent_jobs
            DROP COLUMN turn_source,
            DROP COLUMN goal_round,
            DROP COLUMN goal_round_kind,
            DROP COLUMN goal_revision,
            DROP COLUMN goal_report,
            DROP COLUMN goal_reason
        """
    )
    op.execute(
        """
        ALTER TABLE agent_threads
            DROP CONSTRAINT ck_agent_threads_goal_shape,
            DROP COLUMN goal_objective,
            DROP COLUMN goal_state,
            DROP COLUMN goal_pause_reason,
            DROP COLUMN goal_blocked_code,
            DROP COLUMN goal_blocked_message,
            DROP COLUMN goal_revision,
            DROP COLUMN goal_max_rounds,
            DROP COLUMN goal_blocked_streak,
            DROP COLUMN goal_created_at,
            DROP COLUMN goal_updated_at
        """
    )
