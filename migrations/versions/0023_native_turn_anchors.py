"""Persist exact native turn ordinals for historical session forks.

Platform job state alone is not a stable native-history index: a worker can
settle the CLI context and then lose its lease before finalizing the job.  The
next successful turn must count that already-written native exchange when it
resolves a provider-specific fork boundary.

Revision ID: 0023_native_turn_anchors
Revises: 0022_process_exit_counters
"""

from __future__ import annotations

from alembic import op

revision = "0023_native_turn_anchors"
down_revision = "0022_process_exit_counters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the ordinal and guard old writers in one short schema revision."""
    # Refuse and let the deploy retry while the old API keeps serving instead
    # of waiting indefinitely for an ACCESS EXCLUSIVE lock.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS native_turn_no INTEGER"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'agent_jobs'::regclass
                  AND conname = 'ck_agent_jobs_native_turn_no_positive'
            ) THEN
                ALTER TABLE agent_jobs
                ADD CONSTRAINT ck_agent_jobs_native_turn_no_positive
                CHECK (native_turn_no > 0) NOT VALID;
            END IF;
        END
        $$
        """
    )
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
    op.execute("DROP TRIGGER IF EXISTS trg_agent_job_native_turn_no ON agent_jobs")
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
        "DROP TRIGGER IF EXISTS trg_snapshot_anchor_native_turn_no "
        "ON agent_sandbox_snapshots"
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


def downgrade() -> None:
    """Remove historical native fork ordinals."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_snapshot_anchor_native_turn_no "
        "ON agent_sandbox_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS assign_snapshot_anchor_native_turn_no()")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_job_native_turn_no ON agent_jobs")
    op.execute("DROP FUNCTION IF EXISTS assign_agent_job_native_turn_no()")
    op.execute(
        "ALTER TABLE agent_jobs "
        "DROP CONSTRAINT IF EXISTS ck_agent_jobs_native_turn_no_positive"
    )
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS native_turn_no")
