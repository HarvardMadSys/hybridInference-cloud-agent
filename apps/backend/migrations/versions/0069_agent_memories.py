"""Store each durable memory as its own row.

Revision ID: 0069_agent_memories
Revises: 0068_sandbox_checkpoints
"""

from alembic import op

revision = "0069_agent_memories"
down_revision = "0068_sandbox_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add durable memories; the table starts empty."""
    op.execute(
        """
        CREATE TABLE agent_memories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            repo TEXT,
            content TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            origin TEXT NOT NULL,
            source_job_ids TEXT[] NOT NULL DEFAULT '{}',
            superseded_by TEXT REFERENCES agent_memories (id) ON DELETE SET NULL,
            version BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            archived_at TIMESTAMPTZ,
            CONSTRAINT ck_agent_memories_repo CHECK (repo IS NULL OR repo ~ '[^[:space:]]'),
            CONSTRAINT ck_agent_memories_content CHECK (char_length(content) BETWEEN 1 AND 240),
            CONSTRAINT ck_agent_memories_description
                CHECK (char_length(description) BETWEEN 1 AND 80),
            CONSTRAINT ck_agent_memories_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_agent_memories_origin
                CHECK (origin IN ('agent', 'manual')),
            CONSTRAINT ck_agent_memories_archived
                CHECK ((status = 'archived') = (archived_at IS NOT NULL)),
            CONSTRAINT ck_agent_memories_version CHECK (version >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_memories_scope ON agent_memories (user_id, repo, status)"
    )


def downgrade() -> None:
    """Remove the table this revision added."""
    op.execute("DROP TABLE agent_memories")
