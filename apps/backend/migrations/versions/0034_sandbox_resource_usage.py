"""Store the latest fenced resource observation for each retained container."""

from alembic import op

revision = "0034_sandbox_resource_usage"
down_revision = "0033_compute_node_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add optional monitoring snapshots without changing sandbox ownership."""
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            ADD COLUMN resource_snapshot JSONB,
            ADD COLUMN resource_sampled_at TIMESTAMPTZ,
            ADD COLUMN resource_received_at TIMESTAMPTZ,
            ADD COLUMN terminal_count INTEGER CHECK (terminal_count >= 0),
            ADD COLUMN terminal_count_observed_at TIMESTAMPTZ,
            ADD CONSTRAINT sandbox_resource_snapshot_object
                CHECK (resource_snapshot IS NULL OR jsonb_typeof(resource_snapshot) = 'object'),
            ADD CONSTRAINT sandbox_resource_snapshot_times
                CHECK ((resource_snapshot IS NULL) = (resource_sampled_at IS NULL)
                   AND (resource_snapshot IS NULL) = (resource_received_at IS NULL))
        """
    )


def downgrade() -> None:
    """Remove diagnostic snapshots; retained containers are unaffected."""
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            DROP CONSTRAINT sandbox_resource_snapshot_times,
            DROP CONSTRAINT sandbox_resource_snapshot_object,
            DROP COLUMN terminal_count_observed_at,
            DROP COLUMN terminal_count,
            DROP COLUMN resource_received_at,
            DROP COLUMN resource_sampled_at,
            DROP COLUMN resource_snapshot
        """
    )
