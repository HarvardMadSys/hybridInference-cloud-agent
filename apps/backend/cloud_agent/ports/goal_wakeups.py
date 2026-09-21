"""Port for the durable goal-scheduling queue.

A wakeup says "this goal needs to be looked at", and nothing more. It is not an
authorization to run: it names the goal identity and revision it was created
for, and a consumer that finds either has moved discards it. That is what makes
the queue safe to replay after a crash, and what stops a wakeup that outlived
its goal from resurrecting it.

Its own port rather than a handful of methods on :mod:`cloud_agent.ports.goals`
because it is a different subject with a different lifecycle: the goal is what
the conversation is working toward, the queue is the obligation to look at it,
and only the second one is consumed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.goals.models import GoalWakeupTrigger


class GoalWakeupRepository(Protocol):
    """The durable "this goal needs to be looked at" queue.

    A wakeup is an *obligation to decide*, never an authorization to run: it
    names the goal identity and revision it was created for, and a consumer that
    finds either has moved since discards it. That is what makes the queue safe
    to replay after a crash, and what keeps a cancelled or replaced goal from
    being resurrected by a wakeup that outlived it.
    """

    def transaction(self) -> Any:
        """One transaction, over one connection, for a whole decision.

        The controller holds this across "read the goal under its lock → create
        the round → mark the wakeup processed". Anything less and a crash
        between the last two leaves either a round nobody accounted for or a
        pending wakeup that would create a second one.

        It is on this port because every method here takes the connection it
        produces: the queue is the only subject whose work spans a transaction
        the caller owns end to end.
        """
        ...

    async def arm_wakeup(
        self,
        *,
        thread_id: str,
        goal_id: str | None,
        revision: int,
        trigger: GoalWakeupTrigger,
        trigger_ref: str,
        waiting_job_id: str | None = None,
        conn: Any | None = None,
    ) -> str | None:
        """Record the obligation, or return ``None`` if it already exists.

        Idempotent on ``(thread, goal_id, revision, trigger, trigger_ref)``: a
        replayed finish or a retried owner write finds the wakeup it already
        made instead of making a second one. ``conn`` lets a caller that is
        already inside the transaction that moved the goal arm the wakeup in
        that same commit.
        """
        ...

    async def claim_wakeups(self, *, limit: int = 1) -> list[dict[str, Any]]:
        """Lease up to ``limit`` due wakeups and return them.

        A lease is short and the work under it is one decision against the
        database, never a network call.
        """
        ...

    async def release_wakeup(
        self,
        *,
        wakeup_id: str,
        waiting_job_id: str | None = None,
        retry_in_seconds: float | None = None,
        detail: str | None = None,
        conn: Any | None = None,
    ) -> None:
        """Give a lease back, optionally parking it behind a turn.

        Parking is not a failure: "a person's turn is already waiting, judge
        this again when it ends" is the ordinary answer, and the turn's finish
        will clear ``waiting_job_id``. ``retry_in_seconds`` is for the other
        case — a temporary scheduling failure — and it defers the row's
        ``not_before`` rather than its lease, so a backoff and a dead worker's
        expiry never look alike.
        """
        ...

    async def finish_wakeup(
        self,
        *,
        wakeup_id: str,
        processed: bool = True,
        detail: str | None = None,
        conn: Any | None = None,
    ) -> None:
        """Mark a wakeup processed, or record a permanent refusal.

        A refusal is still terminal: a goal that cannot be scheduled must be
        paused with a reason, not retried behind an "active" badge. The row
        stays as the audit record of what was decided.
        """
        ...

    async def pending_wakeup(self, *, thread_id: str) -> dict[str, Any] | None:
        """The newest unprocessed wakeup of a conversation, if any."""
        ...
