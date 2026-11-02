"""Restore nonnegative guards on durable authority counters.

Revision ID: 0059_counter_constraints
Revises: 0058_mcp_connector_invariant
"""

from alembic import op

revision = "0059_counter_constraints"
down_revision = "0058_mcp_connector_invariant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Reject invalid refresh generations and publication attempt counts."""
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            ADD CONSTRAINT ck_agent_gitlab_connections_refresh_generation
            CHECK (refresh_generation >= 0) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
            ADD CONSTRAINT ck_agent_sandbox_snapshots_publish_attempt_count
            CHECK (publish_attempt_count >= 0) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            VALIDATE CONSTRAINT ck_agent_gitlab_connections_refresh_generation
        """
    )
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
            VALIDATE CONSTRAINT ck_agent_sandbox_snapshots_publish_attempt_count
        """
    )


def downgrade() -> None:
    """Remove only the restored counter guards."""
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
            DROP CONSTRAINT ck_agent_sandbox_snapshots_publish_attempt_count
        """
    )
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            DROP CONSTRAINT ck_agent_gitlab_connections_refresh_generation
        """
    )
