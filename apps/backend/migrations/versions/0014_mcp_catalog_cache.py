"""Persist the official MCP directory without touching existing connections.

Revision ID: 0014_mcp_catalog_cache
Revises: 0013_personal_skills
"""

from alembic import op

revision = "0014_mcp_catalog_cache"
down_revision = "0013_personal_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an independent cache; compatible with the previous API during rollout."""
    op.execute("""CREATE TABLE agent_mcp_catalog_cache (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        entries JSONB NOT NULL CHECK (jsonb_typeof(entries) = 'array'),
        synced_at TIMESTAMPTZ NOT NULL
    )""")


def downgrade() -> None:
    """Remove only the re-creatable directory cache."""
    op.execute("DROP TABLE agent_mcp_catalog_cache")
