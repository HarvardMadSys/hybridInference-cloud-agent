"""Accumulate each retained container's measured CPU and memory per UTC day.

Revision ID: 0077_sandbox_usage_days
Revises: 0076_mcp_residency_fence

``agent_sandboxes`` keeps only the latest Docker sample of a container, so
past consumption could not be read back. Each new sample now adds the time it
stands for, and its CPU and memory over that time, to one row per container
and day. Owners read their own rows on the Usage page; nothing is backfilled,
because the samples before this revision were overwritten.

The Usage page also lists an owner's containers and reads their lifecycle
events, so containers gain an index by owner; until now every container read
was by host, identity or idle deadline.
"""

from alembic import op

revision = "0077_sandbox_usage_days"
down_revision = "0076_mcp_residency_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the per-day totals and the owner index the Usage page reads by."""
    op.execute(
        """
        CREATE TABLE agent_sandbox_usage_days (
            sandbox_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            day DATE NOT NULL,
            measured_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
                CHECK (measured_seconds >= 0),
            cpu_core_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
                CHECK (cpu_core_seconds >= 0),
            memory_byte_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
                CHECK (memory_byte_seconds >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (sandbox_id, day),
            CONSTRAINT fk_agent_sandbox_usage_days_owner
                FOREIGN KEY (sandbox_id, user_id) REFERENCES agent_sandboxes (id, user_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_sandbox_usage_days_owner ON agent_sandbox_usage_days (user_id, day)"
    )
    op.execute("CREATE INDEX idx_agent_sandboxes_owner ON agent_sandboxes (user_id)")


def downgrade() -> None:
    """Drop the accumulated totals; the containers themselves are unaffected."""
    op.execute("DROP INDEX IF EXISTS idx_agent_sandboxes_owner")
    op.execute("DROP TABLE IF EXISTS agent_sandbox_usage_days")
