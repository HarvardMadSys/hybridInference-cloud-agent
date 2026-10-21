"""Store each job event's usage projection beside its payload."""

from alembic import op

revision = "0090_job_event_usage_summary"
down_revision = "0089_superseded_removals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable column; rows stored before it are projected when read.

    Nullable and without a default, so adding it rewrites nothing and holds its
    lock only for the catalog update.
    """
    op.execute("ALTER TABLE agent_job_events ADD COLUMN usage_summary JSONB")


def downgrade() -> None:
    """Drop the column; every read falls back to projecting the payload."""
    op.execute("ALTER TABLE agent_job_events DROP COLUMN usage_summary")
