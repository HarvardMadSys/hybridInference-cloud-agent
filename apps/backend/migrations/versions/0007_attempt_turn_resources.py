"""Give one attempt's whole-turn member execution window its own columns.

The existing resource row answers "what did the container sampler see while
this attempt ran". That question could not be answered honestly for a shared
member sandbox and intentionally returns no container totals there. The guest
helper measures something different: one member's own scheduler time and PSS
peak for the whole turn, cumulative and reported through its own endpoint.

Those numbers are a snapshot, not a rollup, so they get their own columns
rather than a rewrite of the aggregate ones. ``turn_summary`` holds the
canonical snake_case body; ``turn_summary_seq`` holds the ``report_seq`` that
was accepted, which is what makes a replayed or reordered checkpoint a no-op
instead of an accumulation; ``turn_summary_received_at`` records when the
accepted snapshot arrived, which is not derivable from a cumulative counter.

All three are nullable and no existing row is backfilled. A turn recorded
before this revision was not measured this way, and a NULL says exactly that;
writing a zero or an empty object would claim a measurement nobody took.

Revision ID: 0007_attempt_turn_resources
Revises: 0006_host_capacity
"""

from __future__ import annotations

from alembic import op

revision = "0007_attempt_turn_resources"
down_revision = "0006_host_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the three nullable snapshot columns, and index the read path."""
    op.execute(
        """
        ALTER TABLE agent_attempt_resources
            ADD COLUMN IF NOT EXISTS turn_summary JSONB,
            ADD COLUMN IF NOT EXISTS turn_summary_seq BIGINT,
            ADD COLUMN IF NOT EXISTS turn_summary_received_at TIMESTAMPTZ
        """
    )
    # The Runs list folds the attempts of one page of jobs, and only attempts
    # that carry a snapshot contribute. The partial predicate keeps that index
    # proportional to measured turns rather than to every attempt ever run,
    # which for a shared-member deployment is close to all of them; the
    # existing idx_agent_attempt_resources_job index alone cannot skip the
    # unmeasured rows before the heap fetch.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_attempt_resources_turn_summary
            ON agent_attempt_resources (job_id, attempt_id)
            WHERE turn_summary IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop the read-path index and the three snapshot columns."""
    op.execute("DROP INDEX IF EXISTS idx_agent_attempt_resources_turn_summary")
    op.execute(
        """
        ALTER TABLE agent_attempt_resources
            DROP COLUMN IF EXISTS turn_summary,
            DROP COLUMN IF EXISTS turn_summary_seq,
            DROP COLUMN IF EXISTS turn_summary_received_at
        """
    )
