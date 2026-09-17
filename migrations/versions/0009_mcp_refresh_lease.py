"""Give a credential refresh its own lease.

``claim_mcp_refresh`` fenced a refresh by bumping ``credential_version``, which
made two callers holding the same version mutually exclusive and nothing else.
The bump is a *write*, not a lock: it is visible the moment the claim commits,
so the next caller to read the row claims it straight away -- while the first
refresher is still on the network with the refresh token both of them read. An
authorization server that rotates tokens invalidates the first one, so the
second call destroys the connection both were trying to keep.

The lease is the missing state, and it is deliberately a separate pair of
columns rather than a reinterpretation of the version:

- ``refresh_generation`` is bumped by each successful claim and handed back to
  the worker, so the write that lands later can prove it is still the one that
  holds the lease. A claim that was superseded cannot overwrite the credential
  its successor is about to write.
- ``refresh_lease_expires_at`` is what makes exclusivity real *and* keeps the
  property the version bump was chosen for: a refresher that dies mid-flight
  leaves a lease, not a permanent lock, and the row becomes claimable again when
  it expires. The retry is bounded by the lease rather than immediate, which is
  the whole difference.

``credential_version`` goes back to meaning what its name says: it moves when a
credential is written, so a cached protocol session can tell that the token it
opened with was replaced, and it no longer moves for a claim that wrote nothing.
The same shape already fences the attempt-ledger refresh in
``agent_attempt_ledger``; this table gets it for the same reason.

Revision ID: 0009_mcp_refresh_lease
Revises: 0008_generic_mcp_connections
"""

from __future__ import annotations

from alembic import op

revision = "0009_mcp_refresh_lease"
down_revision = "0008_generic_mcp_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the lease generation and its expiry to every connection row."""
    op.execute(
        "ALTER TABLE agent_mcp_connections "
        "ADD COLUMN refresh_generation BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections "
        "ADD CONSTRAINT ck_agent_mcp_connections_refresh_generation "
        "CHECK (refresh_generation >= 0)"
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN refresh_lease_expires_at TIMESTAMPTZ"
    )
    # Partial, because the table is mostly rows nobody is refreshing: the
    # claim's predicate reads this column on every attempt and the index is
    # only worth carrying for the handful of rows that hold a lease.
    op.execute(
        "CREATE INDEX idx_agent_mcp_connections_refresh_lease "
        "ON agent_mcp_connections (refresh_lease_expires_at) "
        "WHERE refresh_lease_expires_at IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the lease; the credential version fence is what remains."""
    op.execute("DROP INDEX IF EXISTS idx_agent_mcp_connections_refresh_lease")
    op.execute(
        "ALTER TABLE agent_mcp_connections "
        "DROP CONSTRAINT IF EXISTS ck_agent_mcp_connections_refresh_generation"
    )
    op.execute("ALTER TABLE agent_mcp_connections DROP COLUMN refresh_lease_expires_at")
    op.execute("ALTER TABLE agent_mcp_connections DROP COLUMN refresh_generation")
