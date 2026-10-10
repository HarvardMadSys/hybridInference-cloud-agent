"""Let a newly allocated node register its SSH host keys with a one-time token.

The control plane must know a new node's host key before its first SSH
connection, and an automatically replaced node has nobody to verify it by
hand. The node's boot script receives a one-time token through the provider
and posts its host keys over HTTPS; only the token's hash is stored here.

A registration belongs to one allocation, expires, and accepts one set of
keys: a retry with the same keys gets the same answer, different keys are
refused.
"""

from __future__ import annotations

from alembic import op

revision = "0030_compute_node_registration"
down_revision = "0029_work_available_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the registration table."""
    op.execute(
        """
        CREATE TABLE agent_compute_node_registrations (
            id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES agent_compute_allocations(id),
            operation_id TEXT NOT NULL REFERENCES agent_compute_operations(id),
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            host_keys JSONB,
            registered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK ((host_keys IS NULL) = (registered_at IS NULL)),
            CHECK (host_keys IS NULL OR jsonb_typeof(host_keys) = 'array')
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_compute_node_registrations_allocation "
        "ON agent_compute_node_registrations(allocation_id, created_at)"
    )


def downgrade() -> None:
    """Drop it; nodes are trusted through administrator-verified host keys only."""
    op.execute("DROP TABLE IF EXISTS agent_compute_node_registrations")
