"""Keep MCP permission revisions unique after replacement or reattachment.

Rebuilding a selection used to reset each policy revision to one. Old approvals
could then match changed permissions, and removing and reattaching a connection
could reuse the same revision. A database sequence provides fresh revisions
across workers and deleted bindings; unchanged selections keep their revision.

Revision ID: 0010_mcp_policy_revisions
Revises: 0009_mcp_refresh_lease
"""

from __future__ import annotations

from alembic import op

revision = "0010_mcp_policy_revisions"
down_revision = "0009_mcp_refresh_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allocate subsequent permission revisions above every persisted fence."""
    op.execute("CREATE SEQUENCE agent_mcp_policy_revision_seq AS BIGINT")
    op.execute(
        "SELECT setval('agent_mcp_policy_revision_seq', GREATEST(1, "
        "COALESCE((SELECT MAX(policy_revision) FROM agent_mcp_thread_bindings), 1), "
        "COALESCE((SELECT MAX(policy_revision) FROM agent_mcp_invocations), 1)))"
    )


def downgrade() -> None:
    """Remove the allocator while preserving existing permission revisions."""
    op.execute("DROP SEQUENCE agent_mcp_policy_revision_seq")
