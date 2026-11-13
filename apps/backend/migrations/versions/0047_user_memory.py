"""Persist owner-scoped portraits, memories and improvement suggestions.

Revision ID: 0047_user_memory
Revises: 0046_job_claim_retry
"""

from alembic import op

revision = "0047_user_memory"
down_revision = "0046_job_claim_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add derived profiles and the watermark that makes forgetting durable."""
    op.execute(
        """
        CREATE TABLE agent_user_memories (
            user_id TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            portrait JSONB NOT NULL,
            memories JSONB NOT NULL DEFAULT '[]'::jsonb,
            suggestions JSONB NOT NULL DEFAULT '[]'::jsonb,
            activity_digest TEXT NOT NULL DEFAULT '',
            cleared_message_id BIGINT NOT NULL DEFAULT 0 CHECK (cleared_message_id >= 0),
            cleared_at TIMESTAMPTZ,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Remove only derived user-memory data."""
    op.execute("DROP TABLE agent_user_memories")
