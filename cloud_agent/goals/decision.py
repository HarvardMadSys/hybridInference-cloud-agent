"""What happens after a turn, as one function with no I/O in it.

Every rule about automatic continuation lives here, and nowhere else: the
caller's only job is to read the facts into a :class:`GoalContext` and then
write whatever :func:`decide` returns. That split is what makes the rules
testable one at a time, and what keeps the orchestration in
:mod:`cloud_agent.goals.continuation` from growing a second, quieter copy of
them.

Two asymmetries are deliberate and are the whole safety story of the feature:

* A completion claim does not finish anything. It buys one *verification*
  round, which re-runs the check the objective names and reports again; only a
  verification round reporting ``complete`` completes the goal.
* A ``blocked`` report does not block anything until it has been reported in
  ``BLOCKED_STREAK`` consecutive turns. One turn saying "I cannot" is a turn,
  not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cloud_agent.goals.models import (
    BLOCKED_STREAK,
    MAX_REASON_CHARS,
    GoalBlockedCode,
    GoalReportStatus,
    GoalRoundKind,
    GoalState,
)

#: What the platform does next. ``none`` covers every state that is not
#: ``active`` as well as "yield to the message a person already queued".
GoalActionKind = Literal[
    "none", "queue_work_round", "queue_verify_round", "complete", "block", "pause"
]


@dataclass(frozen=True)
class GoalContext:
    """Everything the decision depends on, read by the caller beforehand."""

    state: GoalState
    objective: str
    revision: int
    max_rounds: int
    rounds_used: int
    blocked_streak: int
    # -- the turn that just reached a terminal state -----------------------
    turn_state: str
    turn_source: str
    turn_round_kind: GoalRoundKind | None
    report: GoalReportStatus | None
    #: The one line that came with the report, or the turn's own failure detail
    #: when it is ``turn_state`` that is being explained.
    report_reason: str | None
    #: Another turn of this conversation is already queued or running. A person
    #: has asked for something; automatic work gets out of its way.
    thread_has_queued_turn: bool


@dataclass(frozen=True)
class GoalAction:
    """The one thing to persist, and the counters that go with it."""

    kind: GoalActionKind
    reason_code: str | None = None
    message: str | None = None
    #: The blocked streak as this decision leaves it, to be written back. It is
    #: reported even when ``kind`` is ``queue_work_round`` or ``none``, because
    #: a streak that is not reset when it should be is a goal blocked by three
    #: unrelated turns.
    blocked_streak: int = 0


def decide(ctx: GoalContext) -> GoalAction:
    """The next thing to do about this conversation's goal.

    Read the rules in order; the order *is* the specification.
    """
    # 1. Only an active goal continues itself. Paused, blocked and complete all
    #    mean "the platform stops", and every one of them needs its owner.
    if ctx.state != "active":
        return GoalAction(kind="none")

    # 2. A turn that did not end workably is never continued through. This is a
    #    person's only guarantee that a broken conversation cannot spend the
    #    rest of its round budget on its own.
    if ctx.turn_state == "failed":
        return GoalAction(
            kind="pause",
            reason_code="turn-failed",
            message=(ctx.report_reason or "")[:MAX_REASON_CHARS] or None,
        )
    if ctx.turn_state == "cancelled":
        return GoalAction(kind="pause", reason_code="cancelled")

    # 3. Streak accounting. Everything except ``blocked`` clears it: three
    #    blocked reports have to be consecutive to mean anything.
    streak = ctx.blocked_streak + 1 if ctx.report == "blocked" else 0

    # 4. An accepted block. Below the threshold the turn is treated as a
    #    continue, and the next round's prompt carries the un-accepted claim so
    #    the model tries something else rather than repeating itself.
    if ctx.report == "blocked" and streak >= BLOCKED_STREAK:
        return GoalAction(
            kind="block",
            reason_code="model-reported",
            message=(ctx.report_reason or "")[:MAX_REASON_CHARS] or None,
            blocked_streak=streak,
        )

    # 5. Yield. Something is already waiting in this conversation, and it was
    #    asked for by a person; the goal is judged again when it finishes.
    if ctx.thread_has_queued_turn:
        return GoalAction(kind="none", blocked_streak=streak)

    # 6. A completion claim buys one verification round — unless this *is* the
    #    verification round, which is the only thing that can complete a goal.
    if ctx.report == "complete":
        if ctx.turn_round_kind == "verify":
            return GoalAction(kind="complete")
        return GoalAction(kind="queue_verify_round")

    # 7. The round budget. Checked *after* the verification round above, so a
    #    completion claim made on the last permitted round is still verified:
    #    verification rounds are not charged to the budget.
    if ctx.rounds_used >= ctx.max_rounds:
        return GoalAction(
            kind="block",
            reason_code="round-limit",
            message=(
                f"the goal used all {ctx.max_rounds} automatic rounds without reporting completion"
            ),
            blocked_streak=streak,
        )

    # 8. Ordinary continuation. ``report`` is ``continue``, absent, or an
    #    as-yet-unaccepted ``blocked``.
    return GoalAction(kind="queue_work_round", blocked_streak=streak)


# Re-exported so that a reader of this module does not have to guess which
# literal set ``reason_code`` is drawn from.
__all__ = ["GoalAction", "GoalActionKind", "GoalBlockedCode", "GoalContext", "decide"]
