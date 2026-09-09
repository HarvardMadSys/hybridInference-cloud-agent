"""Keep native turn ordinals complete while the previous API is still live.

The host and control-plane deployment jobs intentionally start in parallel.
Migration 0023 therefore adds the column and old-writer triggers in one short
transaction. This following revision performs the historical backfill and
concurrent unique index without retaining the schema lock while an
already-running previous-version control plane continues to settle turns.

Revision ID: 0024_native_turn_rollout_guard
Revises: 0023_native_turn_anchors
"""

from __future__ import annotations

from alembic import op

revision = "0024_native_turn_rollout_guard"
down_revision = "0023_native_turn_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill ordinals without retaining 0023's exclusive schema lock."""
    # 0023 committed the column and old-writer triggers in its own short
    # transaction. Normal turns are succeeded. Snapshot membership additionally
    # identifies the rare turn whose native context settled before final job
    # state was lost. The recursive arm carries that fact into already-forked
    # display copies.
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
        "ALTER TABLE agent_jobs "
        "VALIDATE CONSTRAINT ck_agent_jobs_native_turn_no_positive"
    )
    # PostgreSQL forbids CONCURRENTLY inside a transaction. Alembic's
    # autocommit block ends this revision's backfill transaction first, so the
    # index never takes an ACCESS EXCLUSIVE lock over the live API.
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an INVALID index behind. PostgreSQL
        # considers that name present, so CREATE ... IF NOT EXISTS would skip
        # it on Alembic retry and could let the revision be stamped without an
        # enforcing index. This revision is still unstamped on such a retry;
        # replacing its own index is therefore safe and deterministic.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_jobs_native_turn")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_jobs_native_turn "
            "ON agent_jobs(thread_id, native_turn_no) "
            "WHERE native_turn_no IS NOT NULL"
        )


def downgrade() -> None:
    """Remove the backfill index; 0023 owns the column and writer guard."""
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_jobs_native_turn")
