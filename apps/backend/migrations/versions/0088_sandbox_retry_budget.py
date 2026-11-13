"""Bound automatic sandbox deletion retries without discarding receipts."""

from alembic import op

revision = "0088_sandbox_retry_budget"
down_revision = "0087_sandbox_deletion_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist the automatic retry epoch and number of claimed attempts."""
    op.execute(
        "ALTER TABLE agent_sandboxes "
        "ADD COLUMN deletion_retry_started_at TIMESTAMPTZ, "
        "ADD COLUMN deletion_retry_count INTEGER NOT NULL DEFAULT 0 "
        "CHECK (deletion_retry_count BETWEEN 0 AND 8), "
        "ADD COLUMN deletion_retry_exhausted_at TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE agent_sandboxes SET deletion_retry_started_at = "
        "COALESCE(deletion_requested_at, NOW()) "
        "WHERE deletion_state IN ('pending', 'archived')"
    )


def downgrade() -> None:
    """Remove retry budget state; existing request and archive receipts remain."""
    op.execute(
        "ALTER TABLE agent_sandboxes "
        "DROP COLUMN deletion_retry_exhausted_at, DROP COLUMN deletion_retry_count, "
        "DROP COLUMN deletion_retry_started_at"
    )
