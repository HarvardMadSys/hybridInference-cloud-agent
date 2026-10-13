"""Persist deferred Always allow decisions with their MCP approvals.

Revision ID: 0064_mcp_remembered_approvals
Revises: 0063_user_profiles
"""

from alembic import op

revision = "0064_mcp_remembered_approvals"
down_revision = "0063_user_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep permission intent durable until fenced application or discard."""
    op.execute("""
        ALTER TABLE agent_mcp_invocations
        ADD COLUMN always_allow_rule JSONB,
        ADD COLUMN always_allow_status TEXT,
        ADD CONSTRAINT ck_mcp_remembered_approval CHECK (
            (always_allow_rule IS NULL AND always_allow_status IS NULL) OR
            (always_allow_rule IS NOT NULL AND always_allow_status IS NOT NULL
             AND always_allow_status IN ('pending', 'applied', 'discarded'))
        )
    """)
    op.execute("""
        CREATE INDEX idx_mcp_remembered_approvals
        ON agent_mcp_invocations (thread_id, connection_id)
        WHERE always_allow_status = 'pending'
    """)


def downgrade() -> None:
    """Remove remembered decisions without altering invocation outcomes."""
    op.execute("DROP INDEX idx_mcp_remembered_approvals")
    op.execute("""
        ALTER TABLE agent_mcp_invocations
        DROP CONSTRAINT ck_mcp_remembered_approval,
        DROP COLUMN always_allow_status,
        DROP COLUMN always_allow_rule
    """)
