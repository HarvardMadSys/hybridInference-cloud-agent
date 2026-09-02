"""Persist exact native turn ordinals for historical session forks.

Platform job state alone is not a stable native-history index: a worker can
settle the CLI context and then lose its lease before finalizing the job.  The
next successful turn must count that already-written native exchange when it
resolves a provider-specific fork boundary.

Revision ID: 0021_native_turn_anchors
Revises: 0020_session_fork_coordinator
"""

from __future__ import annotations

from alembic import op

revision = "0021_native_turn_anchors"
down_revision = "0020_session_fork_coordinator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill each thread's settled native-history ordinal."""
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS native_turn_no INTEGER "
        "CHECK (native_turn_no > 0)"
    )
    # Normal turns are succeeded. Snapshot membership additionally identifies
    # the rare turn whose native context settled before final job state was
    # lost. The recursive arm carries that fact into any already-forked display
    # copy, whose fork_source_job_id points at the corresponding source turn.
    op.execute(
        """
        WITH RECURSIVE native_jobs(id) AS (
            SELECT job.id
            FROM agent_jobs job
            WHERE job.thread_id IS NOT NULL
              AND (
                    job.state = 'succeeded'
                    OR EXISTS (
                        SELECT 1
                        FROM agent_sandbox_snapshots snapshot
                        WHERE snapshot.included_job_ids ? job.id
                    )
              )
            UNION
            SELECT copied.id
            FROM agent_jobs copied
            JOIN native_jobs source ON source.id = copied.fork_source_job_id
        ),
        ranked AS (
            SELECT job.id,
                   ROW_NUMBER() OVER (
                       PARTITION BY job.thread_id ORDER BY job.turn_no, job.created_at, job.id
                   ) AS native_turn_no
            FROM agent_jobs job
            JOIN native_jobs native ON native.id = job.id
        )
        UPDATE agent_jobs job
        SET native_turn_no = ranked.native_turn_no
        FROM ranked
        WHERE job.id = ranked.id AND job.native_turn_no IS NULL
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_native_turn "
        "ON agent_jobs(thread_id, native_turn_no) WHERE native_turn_no IS NOT NULL"
    )


def downgrade() -> None:
    """Remove historical native fork ordinals."""
    op.execute("DROP INDEX IF EXISTS idx_agent_jobs_native_turn")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS native_turn_no")
