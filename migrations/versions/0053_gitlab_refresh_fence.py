"""Fence a GitLab token refresh against reconnect and a second refresher.

Revision ID: 0053_gitlab_refresh_fence
Revises: 0053_project_memory
"""

from alembic import op

revision = "0053_gitlab_refresh_fence"
down_revision = "0053_project_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the generation and lease that fence a GitLab token refresh."""
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            ADD COLUMN refresh_generation INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN refresh_lease_expires_at TIMESTAMPTZ
        """
    )


def downgrade() -> None:
    """Drop the GitLab refresh fence."""
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            DROP COLUMN refresh_lease_expires_at,
            DROP COLUMN refresh_generation
        """
    )
