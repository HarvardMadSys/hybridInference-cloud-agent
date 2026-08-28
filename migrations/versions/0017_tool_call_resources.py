"""Record what each individual tool call consumed, where that is knowable.

The attempt rollup says what a turn cost. It cannot say which call cost it,
and that is the question an owner actually asks when a turn is slow or
expensive — measured on production, six calls out of 278 held two thirds of
all tool time, and every one of them was Bash.

Most calls will never fill this table's counters. The sampler reads once a
second and the median call is 35 ms, so the usual row is a name, a duration
and ``sample_count = 0``: the record that this call was too short to measure,
which is a different statement from having used nothing. Nothing here is
NOT NULL that a measurement could fail to produce.

The ``exclusive``/``shared`` split is the other half of that honesty. Parallel
tool calls share the sandbox's counters and cannot be told apart, so a call
that shared its readings stores them in the shared columns with the number of
calls it shared with. A reader that knows only about exclusive numbers then
shows nothing for those calls, rather than showing one call's row containing
several calls' work.

Revision ID: 0017_tool_call_resources
Revises: 0016_runtime_version_menu
"""

from __future__ import annotations

from alembic import op

revision = "0017_tool_call_resources"
down_revision = "0016_runtime_version_menu"
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
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (attempt_id, call_seq),
            CHECK (overlap_n >= 1),
            -- A number is exclusive or shared, never both: the pair of columns
            -- exists so the distinction cannot be lost, and a row holding both
            -- would have lost it.
            CHECK (cpu_usec_exclusive IS NULL OR cpu_usec_shared IS NULL),
            CHECK (mem_peak_exclusive_bytes IS NULL OR mem_peak_shared_bytes IS NULL)
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
    op.execute(
        "ALTER TABLE agent_attempt_resources ADD COLUMN IF NOT EXISTS tool_time_ms BIGINT"
    )


def downgrade() -> None:
    """Drop the per-call rows. The attempt rollup is unaffected."""
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS tool_time_ms")
    op.execute("ALTER TABLE agent_attempt_resources DROP COLUMN IF EXISTS tool_calls_truncated")
    op.execute("DROP TABLE IF EXISTS agent_tool_call_resources")
