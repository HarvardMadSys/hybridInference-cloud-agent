"""Persist each retained sandbox group's immutable writable-layer limit.

Revision ID: 0041_sandbox_disk_limit
Revises: 0040_pr_merge_archive

Zero means no disk quota. Existing containers predate quota configuration and
retain that unlimited value; later administrator edits only affect new groups.
"""

from alembic import op

revision = "0041_sandbox_disk_limit"
down_revision = "0040_pr_merge_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep legacy groups unlimited and bound new reservations in MiB."""
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            ADD COLUMN disk_limit_mib INTEGER NOT NULL DEFAULT 0
                CHECK (disk_limit_mib BETWEEN 0 AND 16777216)
        """
    )


def downgrade() -> None:
    """Remove recorded disk quotas without modifying retained containers."""
    op.execute("ALTER TABLE agent_sandboxes DROP COLUMN IF EXISTS disk_limit_mib")
