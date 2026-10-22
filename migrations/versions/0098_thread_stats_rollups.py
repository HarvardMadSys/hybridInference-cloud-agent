"""Persist versioned per-attempt inputs to the owner thread-stats summary.

Revision ID: 0098_thread_stats_rollups
Revises: 0097_admin_sandbox_deltas

The event ledger remains authoritative and append-only. This table is only a
derived cache: the reader compares ``event_seq`` with the newest sequence in
the ledger and rebuilds a missing, stale, or differently versioned rollup.
Existing attempts are populated lazily, so the migration neither scans nor
locks historical event rows.
"""

from alembic import op

revision = "0098_thread_stats_rollups"
down_revision = "0097_admin_sandbox_deltas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the derived attempt-rollup cache without rewriting history."""
    op.execute(
        """
        CREATE TABLE agent_attempt_thread_stats (
            attempt_id BIGINT PRIMARY KEY
                REFERENCES agent_attempts(id) ON DELETE CASCADE,
            event_seq INTEGER NOT NULL CHECK (event_seq > 0),
            reducer_version INTEGER NOT NULL CHECK (reducer_version > 0),
            rollup JSONB NOT NULL CHECK (jsonb_typeof(rollup) = 'object'),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Drop only the rebuildable thread-stat rollup cache."""
    op.execute("DROP TABLE agent_attempt_thread_stats")
