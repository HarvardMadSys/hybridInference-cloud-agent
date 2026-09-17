"""Personal skill libraries and immutable per-turn selections.

Revision ID: 0013_personal_skills
Revises: 0012_host_concurrency
"""

from __future__ import annotations

from alembic import op

revision = "0013_personal_skills"
down_revision = "0012_host_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add library storage without changing previous release projections."""
    op.execute("""
        CREATE TABLE agent_skills (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
            description TEXT NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1024),
            instructions TEXT NOT NULL CHECK (octet_length(instructions) BETWEEN 1 AND 65536),
            source TEXT NOT NULL CHECK (source IN ('created', 'uploaded', 'github')),
            source_url TEXT,
            source_revision TEXT,
            default_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_skills_owner_name "
        "ON agent_skills (user_id, lower(name)) WHERE deleted_at IS NULL"
    )
    # NULL is an untouched legacy selection; [] explicitly clears the skills
    # in an already-running native conversation. No FK: a past turn owns its
    # content even after the library entry has been edited or removed.
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN skills JSONB "
        "CHECK (skills IS NULL OR jsonb_typeof(skills) = 'array')"
    )


def downgrade() -> None:
    """Remove the new feature's data; only downgrade with the API stopped."""
    op.execute("ALTER TABLE agent_jobs DROP COLUMN skills")
    op.execute("DROP TABLE agent_skills")
