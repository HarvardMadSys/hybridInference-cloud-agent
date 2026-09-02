"""Keep native turn ordinals complete while the previous API is still live.

The host and control-plane deployment jobs intentionally start in parallel.
Migration 0021 can therefore finish while an already-running previous-version
control plane is still settling turns. That writer cannot name the new
``native_turn_no`` column, so the database must fill it until the target API is
the only writer.

Revision ID: 0022_native_turn_rollout_guard
Revises: 0021_native_turn_anchors
"""

from __future__ import annotations

from alembic import op

revision = "0022_native_turn_rollout_guard"
down_revision = "0021_native_turn_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Assign ordinals for old success, snapshot, and native-fork writes."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assign_agent_job_native_turn_no()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.thread_id IS NULL OR NEW.native_turn_no IS NOT NULL THEN
                RETURN NEW;
            END IF;
            IF NEW.state <> 'succeeded'
               AND NOT EXISTS (
                    SELECT 1
                    FROM agent_sandbox_snapshots snapshot
                    WHERE snapshot.included_job_ids ? NEW.id
               )
               AND NOT EXISTS (
                    SELECT 1
                    FROM agent_jobs source
                    WHERE source.id = NEW.fork_source_job_id
                      AND source.native_turn_no IS NOT NULL
               ) THEN
                RETURN NEW;
            END IF;

            PERFORM pg_advisory_xact_lock(
                hashtextextended('agent-native-turn:' || NEW.thread_id, 0)
            );
            SELECT COUNT(*) + 1
            INTO NEW.native_turn_no
            FROM agent_jobs job
            WHERE job.thread_id = NEW.thread_id
              AND job.id <> NEW.id
              AND (job.turn_no, job.created_at, job.id)
                    < (NEW.turn_no, NEW.created_at, NEW.id)
              AND (
                    job.native_turn_no IS NOT NULL
                    OR job.state = 'succeeded'
                    OR EXISTS (
                        SELECT 1
                        FROM agent_sandbox_snapshots snapshot
                        WHERE snapshot.included_job_ids ? job.id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM agent_jobs source
                        WHERE source.id = job.fork_source_job_id
                          AND source.native_turn_no IS NOT NULL
                    )
              );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_agent_job_native_turn_no ON agent_jobs
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_job_native_turn_no
        BEFORE INSERT OR UPDATE OF state, fork_source_job_id, native_turn_no
        ON agent_jobs
        FOR EACH ROW
        EXECUTE FUNCTION assign_agent_job_native_turn_no()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assign_snapshot_anchor_native_turn_no()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE agent_jobs
            SET native_turn_no = native_turn_no
            WHERE id = NEW.anchor_job_id
              AND native_turn_no IS NULL;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_snapshot_anchor_native_turn_no
        ON agent_sandbox_snapshots
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_snapshot_anchor_native_turn_no
        AFTER INSERT OR UPDATE OF anchor_job_id, included_job_ids
        ON agent_sandbox_snapshots
        FOR EACH ROW
        EXECUTE FUNCTION assign_snapshot_anchor_native_turn_no()
        """
    )
    # Re-run 0021's backfill after both triggers exist. This catches writes in
    # the interval after that migration's one-time UPDATE and before the first
    # trigger above was installed; later previous-generation writes are caught
    # synchronously by the triggers.
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


def downgrade() -> None:
    """Remove the previous-generation writer guard."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_snapshot_anchor_native_turn_no "
        "ON agent_sandbox_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS assign_snapshot_anchor_native_turn_no()")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_job_native_turn_no ON agent_jobs")
    op.execute("DROP FUNCTION IF EXISTS assign_agent_job_native_turn_no()")
