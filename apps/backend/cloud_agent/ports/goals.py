"""Port for a conversation's completion goal.

Separate from the conversation's own write port because it is a different
subject: the conversation is where work runs, the goal is what the work is
*for*, and the two have different writers. An owner sets and clears a goal
through the API; the platform advances its state from a finished turn's report.
Neither writer may edit the objective on the platform's behalf, which is why
:meth:`GoalRepository.set_goal` is the only statement here that takes an
objective at all.

Every method that can change state is fenced by the goal's ``revision``, and
every method that both changes state and owes a scheduling decision writes the
wakeup in the same transaction. That second half is the point: a goal whose
state moved but whose wakeup did not is a goal that silently stops, and the
window between "this turn ended" and "the next turn exists" is exactly where a
crash would otherwise lose it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.goals.decision import GoalAction
    from cloud_agent.goals.models import GoalInput, GoalPauseReason


class GoalRepository(Protocol):
    """Read and write one conversation's goal, fenced by its revision."""

    async def get_goal(self, *, thread_id: str, conn: Any | None = None) -> dict[str, Any] | None:
        """The conversation's goal row, or ``None`` when it has none.

        ``conn`` runs the read inside a transaction the caller already owns,
        which is what lets a decision re-read the goal under the same lock it
        writes under.
        """
        ...

    async def lock_goal_state(self, *, thread_id: str, conn: Any) -> dict[str, Any] | None:
        """Read the goal inside the caller's transaction, taking its row lock."""
        ...

    async def live_turn_id(self, *, thread_id: str, conn: Any | None = None) -> str | None:
        """The id of the turn that is queued or running right now, if any."""
        ...

    async def get_goal_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The goal of the conversation the job belongs to, or ``None``."""
        ...

    async def get_thread_goal_state(self, *, user_id: str, thread_id: str) -> dict[str, Any] | None:
        """A conversation's goal *and* its control version, owner-checked.

        ``None`` when the conversation is not this owner's. A conversation with
        no goal answers with ``goal=None`` and the revision it is at, because
        the next write has to be compared against something.
        """
        ...

    async def set_goal(
        self,
        *,
        thread_id: str,
        user_id: str,
        goal: GoalInput,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        """Create, replace or edit the goal, returning it or ``None`` if not owned.

        Raises ``GoalConflict`` when ``expected_revision`` no longer matches.
        Replacing the objective mints a new ``goal_id`` and starts the round
        budget over; editing only the cap keeps both. A completed goal set again
        is a new authorization and mints a new identity even when the text is
        identical. A paused goal that is edited stays paused.
        """
        ...

    async def pause_goal(
        self,
        *,
        thread_id: str,
        revision: int,
        reason: GoalPauseReason,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        """``active`` → ``paused``, cancelling queued automatic rounds with it.

        A pause never interrupts a turn that is already running: the owner's
        "stop this" is the stop button, and this is "do not start another one".
        """
        ...

    async def resume_goal(
        self,
        *,
        thread_id: str,
        user_id: str,
        revision: int,
    ) -> dict[str, Any] | None:
        """``paused`` → ``active``, and record the obligation to schedule.

        Returns the goal as written, or ``None`` when the revision moved under
        the caller. The owner's authorization is the trigger: no model report is
        needed to start working again.
        """
        ...

    async def clear_goal(self, *, thread_id: str, user_id: str) -> bool:
        """Remove the goal entirely, cancelling any queued automatic round."""
        ...

    async def apply_action(
        self,
        *,
        thread_id: str,
        revision: int,
        action: GoalAction,
        pause_reason: GoalPauseReason | None = None,
        pause_detail: str | None = None,
        completed_by_job_id: str | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        """Write what the decision returned, if the goal has not moved since.

        Returns the goal as written, or ``None`` when the revision changed — the
        owner edited, paused or cleared the goal while this turn was finishing,
        and their write is the one that stands.
        """
        ...

    async def has_pending_turn(self, *, thread_id: str, conn: Any | None = None) -> bool:
        """Is any turn of this conversation queued or running right now?"""
        ...

    async def sweep_goal_scheduling(self) -> dict[str, list[str]]:
        """Recover goals whose scheduling obligation was lost, and pause the rest.

        Three shapes are looked for, in this order:

        * ``rearmed`` — an ``active`` goal whose parked wakeup is no longer
          waiting on anything, or whose conversation has gone idle with no
          wakeup at all. The decision is made again.
        * ``stalled`` — an ``active`` goal with nothing queued, running or
          pending for longer than :data:`STALLED_AFTER_S`. No path can explain
          it, so it is paused and said so.

        Returns the thread ids by shape, for the caller's log line.
        """
        ...
