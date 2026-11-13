"""Allow reversible inventory removal without deleting retained native state."""

from alembic import op

revision = "0044_admin_sandbox_archive"
down_revision = "0043_automatic_compute_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep visibility independent of the retained sandbox lifecycle."""
    op.execute("ALTER TABLE agent_sandboxes ADD COLUMN archived_at TIMESTAMPTZ")


def downgrade() -> None:
    """Restore the inventory visibility of every retained sandbox."""
    op.execute("ALTER TABLE agent_sandboxes DROP COLUMN archived_at")
