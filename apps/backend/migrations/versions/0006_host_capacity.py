"""Record what each host is actually doing: CPU, memory, retained containers.

The runner pool could already say how much *scheduling* quota a machine had
left — ``slots_total``/``slots_used`` — but nothing about the machine itself.
An operator looking at a host that was refusing work could not tell a box at
99% CPU from a box with a wedged Docker daemon, and the two have opposite
remedies. This revision adds the two things the admin Servers page needs to
tell them apart, and nothing else:

* **The latest observation**, on the host row itself. One row per host, so the
  pool endpoint stays a single indexed read; the page never fans out to a
  monitoring service that does not exist.
* **A bounded history**, one point per host per minute, for the shape of the
  last 24 hours. A current value alone cannot distinguish a machine that
  spiked for thirty seconds from one that has been pinned all afternoon, which
  is the question that decides whether anybody needs to act.

``capacity_epoch`` is the generation of a host's capacity data, rotated when
the host re-enrolls. A rebuilt machine keeps its name, so without a generation
a freshly rebuilt host's chart would silently continue the previous machine's
line. Existing rows are given one here; the default is volatile, so every row
gets its own and no two hosts are ever mistaken for the same generation.

The snapshot columns stay JSONB because they are written by the runner and read
whole by the page. The history table deliberately does **not** copy that JSON:
it stores the scalars it needs, so a 24-hour window is an index scan over
numbers rather than a deserialization of 1,441 documents.

Revision ID: 0006_host_capacity
Revises: 0005_queued_follow_ups
"""

from __future__ import annotations

from alembic import op

revision = "0006_host_capacity"
down_revision = "0005_queued_follow_ups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the latest-snapshot columns, then the bounded history table."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            ADD COLUMN capacity_snapshot JSONB,
            ADD COLUMN capacity_received_at TIMESTAMPTZ,
            ADD COLUMN capacity_sampled_at TIMESTAMPTZ,
            ADD COLUMN capacity_sample_id UUID,
            ADD COLUMN capacity_epoch UUID NOT NULL DEFAULT gen_random_uuid()
        """
    )
    # The cleanup pass deletes by age alone, across every host and epoch, so it
    # needs an index that does not lead with the host.
    op.execute(
        """
        CREATE TABLE agent_host_capacity_history (
            host TEXT NOT NULL
                REFERENCES agent_runner_hosts (host) ON DELETE CASCADE,
            capacity_epoch UUID NOT NULL,
            bucket_at TIMESTAMPTZ NOT NULL,
            sampled_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sample_id UUID NOT NULL,
            linux_boot_id UUID,
            cpu_percent DOUBLE PRECISION,
            cpu_logical_count INTEGER,
            cpu_window_seconds DOUBLE PRECISION,
            cpu_missing_reason TEXT,
            memory_total_bytes BIGINT,
            memory_available_bytes BIGINT,
            memory_missing_reason TEXT,
            PRIMARY KEY (host, capacity_epoch, bucket_at),
            CONSTRAINT uq_agent_host_capacity_history_sample
                UNIQUE (host, capacity_epoch, sample_id),
            CONSTRAINT ck_agent_host_capacity_history_cpu
                CHECK (cpu_percent IS NULL OR (cpu_percent >= 0 AND cpu_percent <= 100)),
            CONSTRAINT ck_agent_host_capacity_history_cpu_count
                CHECK (cpu_logical_count IS NULL OR cpu_logical_count > 0),
            CONSTRAINT ck_agent_host_capacity_history_cpu_window
                CHECK (
                    cpu_window_seconds IS NULL
                    OR (cpu_window_seconds > 0 AND cpu_window_seconds <= 90)
                ),
            CONSTRAINT ck_agent_host_capacity_history_memory_pair
                CHECK (
                    (memory_total_bytes IS NULL AND memory_available_bytes IS NULL)
                    OR (
                        memory_total_bytes IS NOT NULL
                        AND memory_total_bytes > 0
                        AND memory_available_bytes IS NOT NULL
                        AND memory_available_bytes >= 0
                        AND memory_available_bytes <= memory_total_bytes
                    )
                ),
            CONSTRAINT ck_agent_host_capacity_history_reasons
                CHECK (
                    (cpu_missing_reason IS NULL OR length(cpu_missing_reason) <= 64)
                    AND (memory_missing_reason IS NULL OR length(memory_missing_reason) <= 64)
                )
        )
        """
    )
    # The cleanup pass deletes by age alone, across every host and epoch, so it
    # needs an index that does not lead with the host. The primary key already
    # covers the read path, which always names one host and one epoch.
    op.execute(
        """
        CREATE INDEX idx_agent_host_capacity_history_bucket
            ON agent_host_capacity_history (bucket_at)
        """
    )


def downgrade() -> None:
    """Drop the history table, then the snapshot columns."""
    op.execute("DROP TABLE IF EXISTS agent_host_capacity_history")
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            DROP COLUMN IF EXISTS capacity_snapshot,
            DROP COLUMN IF EXISTS capacity_received_at,
            DROP COLUMN IF EXISTS capacity_sampled_at,
            DROP COLUMN IF EXISTS capacity_sample_id,
            DROP COLUMN IF EXISTS capacity_epoch
        """
    )
