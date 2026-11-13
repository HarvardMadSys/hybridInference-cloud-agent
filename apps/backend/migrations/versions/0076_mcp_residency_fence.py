"""Bind an MCP approval to the residency that requested it."""

from alembic import op

revision = "0076_mcp_residency_fence"
down_revision = "0075_runtime_upgrades"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep old attempt-token approvals distinct from residency approvals."""
    op.execute("""
        ALTER TABLE agent_mcp_invocations ADD COLUMN residency_generation BIGINT
            CHECK (residency_generation > 0)
    """)


def downgrade() -> None:
    """Remove the optional residency fence."""
    op.execute("ALTER TABLE agent_mcp_invocations DROP COLUMN residency_generation")
