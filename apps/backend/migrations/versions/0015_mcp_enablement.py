"""Keep account-wide MCP enablement separate from connection health.

Revision ID: 0015_mcp_enablement
Revises: 0014_mcp_catalog_cache
"""

from alembic import op

revision = "0015_mcp_enablement"
down_revision = "0014_mcp_catalog_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing connected servers remain enabled; credentials stay untouched."""
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT TRUE")


def downgrade() -> None:
    """Remove the account preference without changing conversation choices."""
    op.execute("ALTER TABLE agent_mcp_connections DROP COLUMN enabled")
