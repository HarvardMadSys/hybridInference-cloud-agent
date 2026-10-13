"""Count transient snapshot publication attempts.

Revision ID: 0054_snapshot_publish_attempts
Revises: 0053_gitlab_refresh_fence
"""

from alembic import op

revision = "0054_snapshot_publish_attempts"
down_revision = "0053_gitlab_refresh_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remember how many times publication of a snapshot was deferred."""
    op.execute(
        "ALTER TABLE agent_sandbox_snapshots "
        "ADD COLUMN publish_attempt_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    """Drop the publication attempt counter."""
    op.execute("ALTER TABLE agent_sandbox_snapshots DROP COLUMN publish_attempt_count")
