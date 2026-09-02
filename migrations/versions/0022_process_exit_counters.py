"""Store what each guest process consumed, and charge it to the call that ran it.

The process census said which processes a tool call started. It could not
say what they cost: the periodic sampler reads container totals once a
second, and the median call is 35 ms. The guest kernel accounts every task at
its exit, so the exit event now carries CPU time, peak RSS and I/O bytes, and
this revision gives those numbers a home.

Two tables change. Each process row gains the counters the kernel exposed
for its thread group — null until the leader exited, and null for any
counter the kernel was built without, because an unmeasured zero would be
read as "used nothing". Each tool-call row gains the totals over the
processes attributed to that call alone, plus a flag that says when a
process it may own was left out: one shared with an overlapping call, or one
still running when the turn closed. The totals are then floors, and the flag
is how a reader is told.

Revision ID: 0022_process_exit_counters
Revises: 0021_tool_call_resources
"""

from __future__ import annotations

from alembic import op

revision = "0022_process_exit_counters"
down_revision = "0021_tool_call_resources"
branch_labels = None
depends_on = None

_PROCESS_COUNTERS = (
    "cpu_usec",
    "mem_peak_bytes",
    "io_read_bytes",
    "io_write_bytes",
    "io_rchar_bytes",
    "io_wchar_bytes",
)

_CALL_COUNTERS = (
    "process_cpu_usec",
    "process_mem_peak_bytes",
    "process_io_read_bytes",
    "process_io_write_bytes",
)


def upgrade() -> None:
    """Add exit-time counters per process, and their per-call totals."""
    for column in _PROCESS_COUNTERS:
        op.execute(
            f"ALTER TABLE agent_process_observations "
            f"ADD COLUMN IF NOT EXISTS {column} BIGINT "
            f"CHECK ({column} IS NULL OR {column} >= 0)"
        )
    for column in _CALL_COUNTERS:
        op.execute(
            f"ALTER TABLE agent_tool_call_resources "
            f"ADD COLUMN IF NOT EXISTS {column} BIGINT "
            f"CHECK ({column} IS NULL OR {column} >= 0)"
        )
    op.execute(
        "ALTER TABLE agent_tool_call_resources "
        "ADD COLUMN IF NOT EXISTS process_resources_partial BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # A call charged with process resources must have had a process to
    # charge. Guarded so a re-run of this revision stays idempotent, like
    # every ADD COLUMN IF NOT EXISTS above.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'agent_tool_call_resources_process_resources_need_processes'
            ) THEN
                ALTER TABLE agent_tool_call_resources
                ADD CONSTRAINT agent_tool_call_resources_process_resources_need_processes
                CHECK (
                    process_count > 0
                    OR (
                        process_cpu_usec IS NULL
                        AND process_mem_peak_bytes IS NULL
                        AND process_io_read_bytes IS NULL
                        AND process_io_write_bytes IS NULL
                        AND NOT process_resources_partial
                    )
                );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the exit-time counters and their per-call totals."""
    op.execute(
        "ALTER TABLE agent_tool_call_resources "
        "DROP CONSTRAINT IF EXISTS agent_tool_call_resources_process_resources_need_processes"
    )
    op.execute(
        "ALTER TABLE agent_tool_call_resources DROP COLUMN IF EXISTS process_resources_partial"
    )
    for column in _CALL_COUNTERS:
        op.execute(f"ALTER TABLE agent_tool_call_resources DROP COLUMN IF EXISTS {column}")
    for column in _PROCESS_COUNTERS:
        op.execute(f"ALTER TABLE agent_process_observations DROP COLUMN IF EXISTS {column}")
