"""Retain an administrator's idle-stop request until the host confirms it."""

from alembic import op

revision = "0037_admin_sandbox_stop"
down_revision = "0036_host_settings_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Distinguish a safe idle stop from orphan cleanup across host heartbeats."""
    op.execute("ALTER TABLE agent_sandboxes ADD COLUMN stop_requested_at TIMESTAMPTZ")


def downgrade() -> None:
    """Remove the administrative intent marker."""
    op.execute("ALTER TABLE agent_sandboxes DROP COLUMN stop_requested_at")
