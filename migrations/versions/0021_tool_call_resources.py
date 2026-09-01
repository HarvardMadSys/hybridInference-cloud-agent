"""Record tool windows and Kata guest process lifecycle observations.

The attempt rollup says what a turn cost. It cannot say which call cost it,
and that is the question an owner actually asks when a turn is slow or
expensive — measured on production, six calls out of 278 held two thirds of
all tool time, and every one of them was Bash.

The periodic resource sampler remains a conservative source for CPU/memory.
Kata guest eBPF is a separate event source: fork/exec/exit events preserve
processes that begin and end between sampler readings. Lost-event and
truncation fields are stored explicitly so an empty process list is evidence
of no process only when ``process_observation_complete`` is true.

The ``exclusive``/``shared`` split is the other half of that honesty. Parallel
tool calls share the sandbox's counters and cannot be told apart, so a call
that shared its readings stores them in the shared columns with the number of
calls it shared with. A reader that knows only about exclusive numbers then
shows nothing for those calls, rather than showing one call's row containing
several calls' work.

Revision ID: 0021_tool_call_resources
Revises: 0020_session_fork_coordinator
"""

from __future__ import annotations

from alembic import op

revision = "0021_tool_call_resources"
down_revision = "0020_session_fork_coordinator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add per-call rows, and the flag that says the list is not all of them."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tool_call_resources (
            attempt_id BIGINT NOT NULL
                REFERENCES agent_attempts(id) ON DELETE CASCADE,
            call_seq INTEGER NOT NULL,
            tool_use_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            -- 'message': the window is when the platform parsed the call's
            -- events, which brackets execution from outside. 'exact': the
            -- sandbox's own hooks marked the boundaries. A reader must be
            -- able to tell them apart, so this is stored, not inferred.
            timing_source TEXT NOT NULL DEFAULT 'message',
            duration_ms BIGINT,
            sample_count INTEGER NOT NULL DEFAULT 0,
            overlap_n INTEGER NOT NULL DEFAULT 1,
            measured_span_ms BIGINT,
            cpu_usec_exclusive BIGINT,
            cpu_usec_shared BIGINT,
            mem_peak_exclusive_bytes BIGINT,
            mem_peak_shared_bytes BIGINT,
            process_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (attempt_id, call_seq),
            CHECK (overlap_n >= 1),
            CHECK (process_count >= 0),
            -- A row has one attribution class across every counter, and that
            -- class must agree with whether its sampled span overlapped.
            CHECK (NOT (
                (cpu_usec_exclusive IS NOT NULL OR mem_peak_exclusive_bytes IS NOT NULL)
                AND
                (cpu_usec_shared IS NOT NULL OR mem_peak_shared_bytes IS NOT NULL)
            )),
            CHECK (
                (overlap_n = 1
                    AND cpu_usec_shared IS NULL
                    AND mem_peak_shared_bytes IS NULL)
                OR
                (overlap_n > 1
                    AND cpu_usec_exclusive IS NULL
                    AND mem_peak_exclusive_bytes IS NULL)
            ),
            -- A single level from a cumulative counter is not consumption.
            CHECK (sample_count >= 2 OR (
                measured_span_ms IS NULL
                AND cpu_usec_exclusive IS NULL
                AND cpu_usec_shared IS NULL
                AND mem_peak_exclusive_bytes IS NULL
                AND mem_peak_shared_bytes IS NULL
            ))
        )
        """
    )
    op.execute(
        "ALTER TABLE agent_attempt_resources "
        "ADD COLUMN IF NOT EXISTS tool_calls_truncated BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # The union of the turn's tool windows, never their sum: parallel calls
    # overlap, and a sum can exceed the turn it belongs to. Nullable because a
    # turn recorded before this shipped has no answer, which is not zero.
    op.execute("ALTER TABLE agent_attempt_resources ADD COLUMN IF NOT EXISTS tool_time_ms BIGINT")
    op.execute("ALTER TABLE agent_attempt_resources ADD COLUMN IF NOT EXISTS process_observer TEXT")
    op.execute(
        "ALTER TABLE agent_attempt_resources "
        "ADD COLUMN IF NOT EXISTS process_observation_complete BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE agent_attempt_resources "
        "ADD COLUMN IF NOT EXISTS process_events_lost BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE agent_attempt_resources "
        "ADD COLUMN IF NOT EXISTS processes_truncated BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_process_observations (
            attempt_id BIGINT NOT NULL
                REFERENCES agent_attempts(id) ON DELETE CASCADE,
            process_seq INTEGER NOT NULL,
            uid BIGINT NOT NULL,
            pid BIGINT NOT NULL,
            tgid BIGINT NOT NULL,
            ppid BIGINT,
            comm TEXT NOT NULL,
            executable TEXT,
            started_offset_ms BIGINT NOT NULL,
            duration_ms BIGINT,
            exit_code INTEGER,
            observed_fork BOOLEAN NOT NULL DEFAULT FALSE,
            observed_exec BOOLEAN NOT NULL DEFAULT FALSE,
            observed_exit BOOLEAN NOT NULL DEFAULT FALSE,
            -- Every plausible call when message-level windows overlap. An
            -- empty array means the process was observed but not attributable
            -- to a tool call; the row is still part of the turn's census.
            tool_call_seqs INTEGER[] NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (attempt_id, process_seq),
            CHECK (uid >= 0),
            CHECK (pid > 0 AND tgid > 0),
            CHECK (ppid IS NULL OR ppid > 0),
            CHECK (started_offset_ms >= 0),
            CHECK (duration_ms IS NULL OR duration_ms >= 0),
            CHECK (exit_code IS NULL OR exit_code BETWEEN 0 AND 255)
        )
        """
    )


def downgrade() -> None:
    """Remove per-call and guest-process observation storage."""
    op.execute("DROP TABLE IF EXISTS agent_process_observations")
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS processes_truncated")
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS process_events_lost")
    op.execute(
        "ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS process_observation_complete"
    )
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS process_observer")
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS tool_time_ms")
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS tool_calls_truncated")
    op.execute("DROP TABLE IF EXISTS agent_tool_call_resources")
