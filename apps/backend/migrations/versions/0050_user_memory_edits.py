"""Keep owner-confirmed memory corrections and removals through refreshes.

Revision ID: 0050_user_memory_edits
Revises: 0049_host_drain_fence
"""

from alembic import op

revision = "0050_user_memory_edits"
down_revision = "0049_host_drain_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add scoped overlays without changing existing generated profiles."""
    op.execute(
        """
        CREATE TABLE agent_user_memory_edits (
            user_id TEXT NOT NULL REFERENCES agent_user_memories (user_id) ON DELETE CASCADE,
            memory_id TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            content TEXT CHECK (content IS NULL OR char_length(content) BETWEEN 1 AND 500),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, memory_id)
        )
        """
    )


def downgrade() -> None:
    """Remove only memory correction and removal overlays."""
    op.execute("DROP TABLE agent_user_memory_edits")
