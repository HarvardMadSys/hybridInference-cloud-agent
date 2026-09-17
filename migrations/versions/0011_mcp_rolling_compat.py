"""Keep the previous API's conversation queries usable during MCP rollout.

The original 0008 dropped/renamed columns before deploy.sh replaced the running
API. If host readiness timed out, that API kept serving against the new schema:
conversation listing, creation and composer configuration failed despite green
liveness checks. 0008 now retains inert compatibility columns for fresh upgrades;
this forward revision repairs databases that already applied its original form.

These columns carry no MCP authority. Provider names cannot be written to them,
and the current API continues to use versioned connection bindings exclusively.
Existing connections, credentials, bindings and approvals are not changed.

Revision ID: 0011_mcp_rolling_compat
Revises: 0010_mcp_policy_revisions
"""

from __future__ import annotations

from alembic import op

revision = "0011_mcp_rolling_compat"
down_revision = "0010_mcp_policy_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Repair old projections without restoring provider-name authorization."""
    op.execute(
        "ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS mcp_servers TEXT[] "
        "DEFAULT '{}'::text[]"
    )
    # The revised 0008 already has this constraint. Replace it in this
    # transaction so both upgrade paths end with the same write protection.
    op.execute(
        "ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS ck_agent_threads_legacy_mcp_empty"
    )
    op.execute(
        "ALTER TABLE agent_threads ADD CONSTRAINT ck_agent_threads_legacy_mcp_empty "
        "CHECK (mcp_servers IS NULL OR cardinality(mcp_servers) = 0)"
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN IF NOT EXISTS server TEXT "
        "GENERATED ALWAYS AS (NULL::text) STORED"
    )


def downgrade() -> None:
    """Restore the original 0010 shape; only downgrade with the API stopped."""
    op.execute("ALTER TABLE agent_threads DROP COLUMN mcp_servers")
    op.execute("ALTER TABLE agent_mcp_connections DROP COLUMN server")
