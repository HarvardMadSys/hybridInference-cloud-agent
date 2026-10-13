"""Persist source-backed memory edits and consolidation overrides.

Revision ID: 0052_memory_overrides
Revises: 0051_host_drain_fence_epoch
"""

from alembic import op

revision = "0052_memory_overrides"
down_revision = "0051_host_drain_fence_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep owner edits separate from automatically rebuilt activity."""
    op.execute(
        "ALTER TABLE agent_user_memories ADD COLUMN overrides JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    # Rebuild older, potentially long saved views under the new bounds once.
    op.execute("UPDATE agent_user_memories SET activity_digest = ''")


def downgrade() -> None:
    """Remove memory edits without touching the reset fence."""
    op.execute("ALTER TABLE agent_user_memories DROP COLUMN overrides")
