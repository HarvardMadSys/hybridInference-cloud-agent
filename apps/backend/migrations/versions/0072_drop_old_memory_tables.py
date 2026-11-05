"""Drop the tables of the retired 50-request memory.

Revision ID: 0072_drop_old_memory_tables
Revises: 0071_temporary_workspaces

``0070_memory_cutover`` copied their switches and preferences onto durable
memories and left them unread so a rollback could still find them. Nothing
reads or writes them now. A downgrade recreates them empty, as 0070 left them:
the rows dropped here do not come back.
"""

from alembic import op

revision = "0072_drop_old_memory_tables"
down_revision = "0071_temporary_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop the edit tables first; they reference the memory tables."""
    op.execute("DROP TABLE agent_user_memory_edits")
    op.execute("DROP TABLE agent_project_memory_edits")
    op.execute("DROP TABLE agent_user_memories")
    op.execute("DROP TABLE agent_project_memories")


def downgrade() -> None:
    """Recreate the four tables empty, with the shape 0070 left them in."""
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
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            overrides JSONB NOT NULL DEFAULT '[]'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_project_memories (
            user_id TEXT NOT NULL,
            repo TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            portrait JSONB NOT NULL,
            memories JSONB NOT NULL DEFAULT '[]'::jsonb,
            suggestions JSONB NOT NULL DEFAULT '[]'::jsonb,
            overrides JSONB NOT NULL DEFAULT '[]'::jsonb,
            activity_digest TEXT NOT NULL DEFAULT '',
            cleared_message_id BIGINT NOT NULL DEFAULT 0 CHECK (cleared_message_id >= 0),
            cleared_at TIMESTAMPTZ,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, repo)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_user_memory_edits (
            user_id TEXT NOT NULL REFERENCES agent_user_memories (user_id) ON DELETE CASCADE,
            memory_id TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            content TEXT CHECK (
                content IS NULL OR (char_length(content) >= 1 AND char_length(content) <= 500)
            ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, memory_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_project_memory_edits (
            user_id TEXT NOT NULL,
            repo TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            content TEXT CHECK (
                content IS NULL OR (char_length(content) >= 1 AND char_length(content) <= 500)
            ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, repo, memory_id),
            FOREIGN KEY (user_id, repo)
                REFERENCES agent_project_memories (user_id, repo) ON DELETE CASCADE
        )
        """
    )
