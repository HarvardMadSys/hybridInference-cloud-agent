"""Live probes that prove a candidate connection reaches this same database.

Two databases can share a system identifier, an OID, a migration revision and
an identity namespace (one restored from the other's backup). What they cannot
share is a write made after they diverged: the control plane writes a random
token here over its own connection, a deployment agent reads it over the
candidate connection and writes a reply, and the control plane reads the reply
back over its own connection. Only the same live database passes.

Revision ID: 0111_database_probes
Revises: 0110_admin_credentials
"""

from __future__ import annotations

from alembic import op

revision = "0111_database_probes"
down_revision = "0110_admin_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the probe table."""
    op.execute(
        """
        CREATE TABLE agent_database_probes (
            probe_id TEXT PRIMARY KEY CHECK (probe_id ~ '^[0-9a-f]{32}$'),
            token TEXT NOT NULL CHECK (token ~ '^[0-9a-f]{64}$'),
            responses JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Drop the probe table."""
    op.execute("DROP TABLE IF EXISTS agent_database_probes")
