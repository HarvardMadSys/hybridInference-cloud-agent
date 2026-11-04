"""MCP connections can run inside the task sandbox (location ``sandbox``).

A ``sandbox`` connection is a pinned stdio server the trusted host runs in the
sandbox's MCP gateway, next to the agent, instead of one the relay dials
(cloud_agent_shared.sandbox_mcp). Only the location's check constraint
changes; a sandbox connection is stored like any stdio one, in
``launch_spec``.

The constraint was declared inline in ``0008_generic_mcp_connections``, so its
name is whatever PostgreSQL chose. It is found by what it checks rather than
by name, and recreated under a stable one. The SQL has no ``%`` in it: some
callers replay migrations through drivers that treat it as a placeholder.

The downgrade refuses while a sandbox connection exists: it would leave rows
the old constraint forbids, and silently deleting a user's connection is not a
downgrade.

Revision ID: 0110_mcp_sandbox_location
Revises: 0109_local_account_parity
"""

from alembic import op

revision = "0110_mcp_sandbox_location"
down_revision = "0109_local_account_parity"
branch_labels = None
depends_on = None

_CONSTRAINT = "agent_mcp_connections_location_check"

_DROP_LOCATION_CHECKS = """
DO $$
DECLARE
    found record;
BEGIN
    FOR found IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'agent_mcp_connections'::regclass
          AND contype = 'c'
          AND position('location' in pg_get_constraintdef(oid)) > 0
          AND position('managed-worker' in pg_get_constraintdef(oid)) > 0
    LOOP
        EXECUTE 'ALTER TABLE agent_mcp_connections DROP CONSTRAINT '
            || quote_ident(found.conname);
    END LOOP;
END
$$
"""


def _add_check(locations: str) -> None:
    op.execute(
        f"ALTER TABLE agent_mcp_connections ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (location IN {locations})"
    )


def upgrade() -> None:
    """Admit ``sandbox`` beside the three existing locations."""
    op.execute(_DROP_LOCATION_CHECKS)
    _add_check("('remote', 'managed-worker', 'user-connector', 'sandbox')")


def downgrade() -> None:
    """Restore the three-location constraint, if no sandbox connection exists."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_mcp_connections WHERE location = 'sandbox') THEN
                RAISE EXCEPTION 'sandbox MCP connections exist; delete them before downgrading';
            END IF;
        END
        $$
        """
    )
    op.execute(_DROP_LOCATION_CHECKS)
    _add_check("('remote', 'managed-worker', 'user-connector')")
