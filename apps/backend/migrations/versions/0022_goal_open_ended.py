"""Let a goal run without a round limit, and keep the limits owners did set.

Revision ID: 0022_goal_open_ended
Revises: 0021_continuous_measurement

``0019`` and ``0020`` made ``goal_max_rounds`` a required half of a goal: the
shape constraint asserted ``(goal_objective IS NULL) = (goal_max_rounds IS
NULL)``, so a goal *had* to carry a number, and ``0019``'s own column check
capped it at fifty. Together with a client-side default of ten, that made every
goal finite — the platform stopped it at a budget nobody had asked for.

This revision removes the requirement. ``NULL`` now means what it says: the
owner set no limit, and the goal continues until it is finished, paused,
cleared, blocked on its owner, or one of the run-time limits that already
governs every turn (the account's inference quota, a single execution's
deadline, cancellation) applies. There is no default and there is no maximum;
"no limit" is the absence of a value, not a large one.

**Nothing is backfilled, and nothing an owner set is removed.** A row carrying
``goal_max_rounds = 10`` keeps it. The old client defaulted to ten, so ten is
suspicious — but it is not proof, the same value is a perfectly ordinary thing
for a caller to have asked for, and a migration that guessed would be silently
deleting a constraint its owner stated. The way out of a spent limit is the
owner's own Resume: it lifts a bound that has been reached, and the API returns
the new bound so the change is visible rather than implied. A bound that has
*not* been reached is left alone, so "at most fifty, paused at three" still
means fifty.

Nothing is resumed either. A goal that was paused stays paused, whatever paused
it: this migration changes what a goal may look like, never what it is doing.
"""

from alembic import op

revision = "0022_goal_open_ended"
down_revision = "0021_continuous_measurement"
branch_labels = None
depends_on = None

#: The shape a goal has from here on.
#:
#: ``goal_max_rounds`` is bounded below but not above, and is optional. The
#: implication runs one way on purpose: an objective may exist with no limit
#: (the ordinary case now), but a limit with no objective would be a bound on
#: nothing, and the columns are cleared together by ``clear_goal``.
_OPEN_ENDED_SHAPE = """
    (goal_objective IS NULL) = (goal_id IS NULL)
    AND (goal_max_rounds IS NULL
         OR (goal_objective IS NOT NULL AND goal_max_rounds >= 1))
    AND (goal_pause_reason IS NOT NULL OR goal_pause_detail IS NULL)
    AND goal_repairs_used >= 0
"""

#: What ``0019``/``0020`` asserted, restored verbatim by ``downgrade``.
_FINITE_SHAPE = """
    (goal_objective IS NULL) = (goal_id IS NULL)
    AND (goal_objective IS NULL) = (goal_max_rounds IS NULL)
    AND (goal_pause_reason IS NOT NULL OR goal_pause_detail IS NULL)
    AND goal_repairs_used >= 0
"""


def upgrade() -> None:
    """Make the round limit optional, without rewriting a single goal."""
    # ``0019`` declared the ceiling inline on the column, so Postgres named it
    # after the table and the column. ``0020`` already dropped it; the guard is
    # here because this revision must stand on its own against a database that
    # was stamped forward by any path.
    op.execute(
        "ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS agent_threads_goal_max_rounds_check"
    )
    op.execute(
        """
        ALTER TABLE agent_threads
            DROP CONSTRAINT ck_agent_threads_goal_shape,
            ADD CONSTRAINT ck_agent_threads_goal_shape CHECK ("""
        + _OPEN_ENDED_SHAPE
        + """
            )
        """
    )


def downgrade() -> None:
    """Require a limit again, filling in the rows that have none.

    The previous shape cannot hold a goal without a number, so the rows this
    revision allowed are given the old default rather than dropped or paused:
    losing a goal's *objective* on a downgrade would be a far worse outcome than
    bounding it, and the objective is what the owner wrote.

    **Nothing else about the bound is touched, and no ceiling is restored.**
    ``0020`` has no upper bound on ``goal_max_rounds`` at all — it dropped the
    inline ``BETWEEN 1 AND 50`` that ``0019`` had declared on the column. A
    downgrade that re-added it would not be restoring ``0020``; it would be
    inventing a check that revision never had, and it would fail outright on a
    bound this service legitimately accepts (there is no maximum any more). What
    is restored here is ``0020``'s constraint, exactly as it shipped.
    """
    op.execute(
        """
        UPDATE agent_threads
        SET goal_max_rounds = 10
        WHERE goal_objective IS NOT NULL AND goal_max_rounds IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE agent_threads
            DROP CONSTRAINT ck_agent_threads_goal_shape,
            ADD CONSTRAINT ck_agent_threads_goal_shape CHECK ("""
        + _FINITE_SHAPE
        + """
            )
        """
    )
