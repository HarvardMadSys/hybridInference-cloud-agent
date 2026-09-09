"""Record what each attempt actually consumed: CPU, memory, io, network.

The platform runs every turn inside a sandbox it owns and, until now, kept no
record of what that turn cost in machine terms — a tool call that pins the host
for ten minutes is indistinguishable from one that sleeps. The observability
design's third signal class (resources) rolls the runner's periodic samples up
into one row per attempt.

``coverage`` is why this is a table rather than a handful of columns: a
measurement we failed to take must be distinguishable from a measured zero, so
every row carries the fraction of its attempt window that was actually sampled
and the sampler build that took it. A host whose backend cannot be read at all
stores ``coverage = 0`` rather than a convincing row of zeros.

Revision ID: 0013_attempt_resources
Revises: 0012_residency_grant
"""

from __future__ import annotations

from alembic import op

revision = "0013_attempt_resources"
down_revision = "0012_residency_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the per-attempt resource rollup."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_attempt_resources (
            attempt_id BIGINT PRIMARY KEY REFERENCES agent_attempts(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL,
            cpu_usec BIGINT,
            mem_peak_bytes BIGINT,
            mem_avg_bytes BIGINT,
            io_read_bytes BIGINT,
            io_write_bytes BIGINT,
            net_rx_bytes BIGINT,
            net_tx_bytes BIGINT,
            sample_count INTEGER NOT NULL DEFAULT 0,
            coverage DOUBLE PRECISION NOT NULL DEFAULT 0,
            sampler_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (coverage >= 0 AND coverage <= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_attempt_resources_job "
        "ON agent_attempt_resources(job_id)"
    )


def downgrade() -> None:
    """Drop the rollup. Nothing else reads it; samples are never retained."""
    op.execute("DROP INDEX IF EXISTS idx_agent_attempt_resources_job")
    op.execute("DROP TABLE IF EXISTS agent_attempt_resources")
