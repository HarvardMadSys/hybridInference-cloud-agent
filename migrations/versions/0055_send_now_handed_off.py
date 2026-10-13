"""Remember a send-now message the host already accepted.

Revision ID: 0055_send_now_handed_off
Revises: 0054_snapshot_publish_attempts
"""

from alembic import op

revision = "0055_send_now_handed_off"
down_revision = "0054_snapshot_publish_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A restarted control plane must not inject a message the host already took."""
    op.execute(
        "ALTER TABLE agent_pending_messages "
        "ADD COLUMN send_now_handed_off BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    """Drop the send-now handoff mark."""
    op.execute("ALTER TABLE agent_pending_messages DROP COLUMN send_now_handed_off")
