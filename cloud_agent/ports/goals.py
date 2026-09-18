"""Port for a conversation's completion goal.

Separate from the conversation's own write port because it is a different
subject: the conversation is where work runs, the goal is what the work is
*for*, and the two have different writers. An owner sets and clears a goal
through the API; the platform advances its state from a worker's terminal
transition. Neither writer may edit the objective on the platform's behalf,
which is why :meth:`GoalRepository.set_goal` is the only statement here that
takes an objective at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.goals.decision import GoalAction
    from cloud_agent.goals.models import GoalBlockedCode, GoalInput, GoalPauseReason


class GoalRepository(Protocol):
    """Read and write one conversation's goal, fenced by its revision."""

    async def get_goal(self, *, thread_id: str) -> dict[str, Any] | None:
        """The conversation's goal row, or ``None`` when it has none."""
        ...

    async def get_goal_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The goal of the conversation the job belongs to, or ``None``."""
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
        The revision advances on every accepted write, so a caller that read
        one can prove nothing changed under it. An objective that is unchanged
        keeps the round counter; a different one starts the budget over.
        """
        ...

    async def pause_goal(self, *, thread_id: str, revision: int, reason: GoalPauseReason) -> bool:
        """``active`` → ``paused``, cancelling queued automatic rounds with it."""
        ...

    async def resume_goal(self, *, thread_id: str, revision: int) -> bool:
        """``paused`` → ``active`` and clear the pause reason."""
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
        blocked_code: GoalBlockedCode | None = None,
    ) -> dict[str, Any] | None:
        """Write what the decision returned, if the goal has not moved since.

        Returns the goal as written, or ``None`` when the revision changed — the
        owner edited, paused or cleared the goal while this turn was finishing,
        and their write is the one that stands. The row is returned rather than
        a flag because a decision can be applied twice in one turn — a blocked
        streak, then the action that streak implies — and the second write has
        to be fenced against the first one's revision.
        """
        ...

    async def record_goal_report(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        status: str,
        reason: str | None,
    ) -> bool:
        """Store one turn's goal report on its job row, under the live lease.

        The same two columns the message parser writes, so a report made
        through a tool and one written as text are the same fact by the time
        anything reads them. Returns ``False`` when the attempt is no longer
        the running one — a superseded turn must not relabel what its successor
        is doing.
        """
        ...

    async def has_pending_turn(self, *, thread_id: str) -> bool:
        """Is any turn of this conversation queued or running right now?"""
        ...

    async def pause_stalled_goals(self) -> list[str]:
        """Pause active goals whose conversation has gone quiet; return their ids."""
        ...
