"""Record how long each execution spent starting its sandbox.

"Time to first token" was the only duration this service kept, and on the
owner-facing surface it is one average across a whole thread: the window from
an attempt starting to its first output event contains the queue wait, the
container's creation, the workspace import, the setup script, the Agent CLI's
own start and the model's first token, with nothing to say which of them
dominated. An operator looking at a slow chat could see that it was slow and
nothing about why; attributing the time meant reading the host's log by hand
and lining up the stages yourself.

What lands here is the host's own account of one activation, measured with a
monotonic clock in the process that did the waiting, and reported as soon as
the prompt reaches the CLI — before the turn runs, so a turn that later times
out still carries the measurement of how it started. It is deliberately not
derived from control-plane receipt timestamps: those add a round trip to
every boundary and cannot see a stage that never produced one.

Three columns rather than a table, because an attempt has exactly one
activation and the admin read already fetches the attempt row:

* ``activation_ms`` — the wall clock the host spent, in total.
* ``activation_stages`` — the same window split by stage. The names come from
  the runner's existing stage vocabulary, the one its failure logs already
  use, so a number here and a log line there name the same thing.
* ``activation_failed_stage`` — the stage that was running when an activation
  failed. This is what makes a partial breakdown readable as partial rather
  than as an activation that finished suspiciously fast.

A total and a breakdown arrive together or not at all; the coherence check
below says so, because a breakdown without its total would let a reader sum
the stages and call the sum the activation.

Historical rows stay NULL. That is the honest reading of "not measured", and
it is why the pair is nullable rather than defaulted to zero.

Revision ID: 0007_attempt_activation_timing
Revises: 0006_host_capacity
"""

from __future__ import annotations

from alembic import op

revision = "0007_attempt_activation_timing"
down_revision = "0006_host_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the activation columns, then the checks that keep them honest."""
    op.execute(
        """
        ALTER TABLE agent_attempts
            ADD COLUMN activation_ms INTEGER,
            ADD COLUMN activation_stages JSONB,
            ADD COLUMN activation_failed_stage TEXT
        """
    )
    # A stage name is written by the host and read back by the admin page; the
    # bound is the same one the wire schema enforces, repeated here so a future
    # writer that skips the schema cannot put an unbounded string on the row.
    op.execute(
        """
        ALTER TABLE agent_attempts
            ADD CONSTRAINT ck_agent_attempts_activation_total
                CHECK (activation_ms IS NULL OR activation_ms >= 0),
            ADD CONSTRAINT ck_agent_attempts_activation_coherent
                CHECK ((activation_ms IS NULL) = (activation_stages IS NULL)),
            ADD CONSTRAINT ck_agent_attempts_activation_stages_object
                CHECK (
                    activation_stages IS NULL
                    OR jsonb_typeof(activation_stages) = 'object'
                ),
            ADD CONSTRAINT ck_agent_attempts_activation_failed_stage
                CHECK (
                    activation_failed_stage IS NULL
                    OR length(activation_failed_stage) BETWEEN 1 AND 64
                )
        """
    )


def downgrade() -> None:
    """Drop the activation columns and the checks that guarded them."""
    op.execute(
        """
        ALTER TABLE agent_attempts
            DROP COLUMN IF EXISTS activation_ms,
            DROP COLUMN IF EXISTS activation_stages,
            DROP COLUMN IF EXISTS activation_failed_stage
        """
    )
