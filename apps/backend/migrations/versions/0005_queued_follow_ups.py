"""Use queued for every submitted turn that has not started.

The scheduler now checks earlier unfinished turns before claiming a job.
Stop the old control-plane writers before applying this revision: they can
still insert waiting and claim queued descendants without that check.

Revision ID: 0005_queued_follow_ups
Revises: 0004_residency_on_membership
"""

from __future__ import annotations

from alembic import op

revision = "0005_queued_follow_ups"
down_revision = "0004_residency_on_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Convert pending turns and index the scheduler's predecessor check."""
    op.execute("LOCK TABLE agent_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute("UPDATE agent_jobs SET state = 'queued' WHERE state = 'waiting'")
    op.execute("ALTER TABLE agent_jobs DROP CONSTRAINT agent_jobs_state_check")
    op.execute(
        "ALTER TABLE agent_jobs ADD CONSTRAINT agent_jobs_state_check "
        "CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))"
    )
    op.execute("DROP INDEX idx_agent_jobs_waiting_parent")
    op.execute("DROP INDEX idx_agent_jobs_pending_thread")
    op.execute(
        "CREATE INDEX idx_agent_jobs_queued_parent ON agent_jobs (parent_job_id) "
        "WHERE state = 'queued'"
    )
    op.execute(
        "CREATE INDEX idx_agent_jobs_pending_thread ON agent_jobs (thread_id) "
        "WHERE state = 'queued'"
    )
    op.execute(
        "CREATE INDEX idx_agent_jobs_unfinished_thread ON agent_jobs (thread_id, turn_no) "
        "WHERE state IN ('queued', 'running')"
    )


def downgrade() -> None:
    """Restore waiting where the old scheduler needs a predecessor gate."""
    op.execute("LOCK TABLE agent_jobs IN ACCESS EXCLUSIVE MODE")
    # The old promoter only checks the direct parent. It cannot safely wait
    # behind an unfinished ancestor through a cancelled intermediate turn.
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM agent_jobs child
                JOIN agent_jobs parent ON parent.id = child.parent_job_id
                WHERE child.state = 'queued'
                  AND parent.state IN ('succeeded', 'failed', 'cancelled')
                  AND EXISTS (
                      SELECT 1 FROM agent_jobs earlier
                      WHERE earlier.thread_id = child.thread_id
                        AND earlier.turn_no < child.turn_no
                        AND earlier.state IN ('queued', 'running')
                  )
            ) THEN
                RAISE EXCEPTION 'Drain queued follow-ups before downgrading: an unfinished '
                    'ancestor is hidden by a terminal parent';
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE agent_jobs DROP CONSTRAINT agent_jobs_state_check")
    op.execute(
        "ALTER TABLE agent_jobs ADD CONSTRAINT agent_jobs_state_check "
        "CHECK (state IN ('queued', 'waiting', 'running', 'succeeded', 'failed', 'cancelled'))"
    )
    op.execute(
        """
        UPDATE agent_jobs child SET state = 'waiting'
        FROM agent_jobs parent
        WHERE child.parent_job_id = parent.id AND child.state = 'queued'
          AND (
              parent.state IN ('queued', 'running')
              OR (
                  parent.state = 'succeeded' AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      JOIN agent_sandboxes g ON g.id = m.sandbox_id
                      WHERE m.thread_id = child.thread_id
                        AND m.context_status <> 'lost'
                        AND m.native_state <> 'lost'
                        AND g.state NOT IN ('stopped_dirty', 'lost')
                  )
              )
          )
        """
    )
    op.execute("DROP INDEX idx_agent_jobs_unfinished_thread")
    op.execute("DROP INDEX idx_agent_jobs_queued_parent")
    op.execute("DROP INDEX idx_agent_jobs_pending_thread")
    op.execute(
        "CREATE INDEX idx_agent_jobs_waiting_parent ON agent_jobs (parent_job_id) "
        "WHERE state = 'waiting'"
    )
    op.execute(
        "CREATE INDEX idx_agent_jobs_pending_thread ON agent_jobs (thread_id) "
        "WHERE state IN ('waiting', 'queued')"
    )
