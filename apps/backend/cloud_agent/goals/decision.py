"""What happens after one turn, as one function with no I/O in it.

Every rule about automatic continuation lives here, and nowhere else: the
caller's only job is to read the facts into a :class:`GoalContext` and then
write whatever :func:`decide` returns. That split is what makes the rules
testable one at a time, and what keeps the orchestration in
:mod:`cloud_agent.goals.controller` from growing a second, quieter copy of
them.

The rules, in the order they are read — the order *is* the specification:

1. Only an ``active`` goal continues itself.
2. The turn has to have ended workably: a failed or cancelled turn never
   contributes a completion.
3. The report has to be readable at all. A report that could not be attributed
   to a complete final message pauses; it is not the same fact as no report.
4. A malformed report earns one repair round, if the runtime can run one.
5. A person's input wins. Nothing is queued in front of it, and a completion
   claim made just before it arrived does not get to finish the goal from
   behind it.
6. A valid terminal report is honored: ``needs_input`` pauses, ``complete``
   completes.
7. The round bound, checked *after* the report, so a valid completion on the
   last permitted round still counts. **There is no bound unless the owner
   stated one.** An unset bound is the ordinary case and stops nothing; this
   rule exists for the owner who asked for a specific number, and for the rows
   written when the platform still imposed a default of its own.
8. Otherwise, work continues — for as long as the goal says it should.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cloud_agent.goals.models import (
    MAX_REASON_CHARS,
    MAX_REPORT_REPAIRS,
    GoalPauseReason,
    GoalRoundKind,
    GoalState,
)
from cloud_agent.goals.report import GoalReport, GoalReportOutcome, GoalReportParse

#: What the platform does next. ``none`` covers every state that is not
#: ``active``, every case where another turn already owns the outcome, and
#: "yield to the message a person already queued".
GoalActionKind = Literal["none", "queue_work_round", "queue_repair_round", "complete", "pause"]


@dataclass(frozen=True)
class GoalContext:
    """Everything the decision depends on, read by the caller beforehand."""

    state: GoalState
    goal_id: str
    revision: int
    #: The owner's own bound, or ``None`` when they stated none — which is the
    #: ordinary case. Never a sentinel: a decision that read ``999999`` here
    #: could not tell "no limit" from "a very large limit".
    max_rounds: int | None
    rounds_used: int
    # -- the turn that just reached a terminal state -----------------------
    turn_job_id: str
    turn_goal_id: str | None
    turn_revision: int | None
    turn_state: str
    turn_round_kind: GoalRoundKind | None
    #: How the turn's final message read: ``absent``, ``valid``, ``invalid`` or
    #: ``unavailable``. ``None`` means no read was attempted, which happens only
    #: on an owner-triggered resume where there is no turn to read.
    report_parse: GoalReportParse | None = None
    report: GoalReport | None = None
    #: The machine-readable reason a report was refused. Only set on ``invalid``.
    report_detail: str | None = None
    #: The turn's own failure detail, when it is ``turn_state`` being explained.
    turn_detail: str | None = None
    #: Another turn of this conversation is already queued or running. A person
    #: has asked for something; automatic work gets out of its way.
    thread_has_queued_turn: bool = False
    #: A person's turn exists that this turn did not come after — accepted while
    #: this turn was running, or already settled. This is what stops a
    #: completion claim from finishing a goal the owner has moved on from.
    thread_has_newer_input: bool = False
    #: Repair rounds already spent on the *current* invalid report.
    repairs_used: int = 0
    #: Whether this runtime can be trusted to run a bounded report repair at all.
    #: An unverified runtime gets no repair round: a repair that can also edit
    #: the repository is not a repair, it is another work round with a nicer
    #: name.
    repair_supported: bool = False
    #: The runtime serving this conversation cannot deliver the report protocol.
    runtime_supported: bool = True


@dataclass(frozen=True)
class GoalAction:
    """The one thing to persist, and the counters that go with it."""

    kind: GoalActionKind
    reason: GoalPauseReason | None = None
    message: str | None = None
    #: ``complete`` only: the name of the evidence the platform accepted.
    completion_basis: str | None = None


def decide(ctx: GoalContext) -> GoalAction:
    """The next thing to do about this conversation's goal."""
    # 1. Only an active goal continues itself. Paused and complete both mean
    #    "the platform stops", and both need their owner.
    if ctx.state != "active":
        return GoalAction(kind="none")

    # 2. A turn that did not end workably is never continued through. A failed
    #    turn reaches this function only once the execution policy has spent its
    #    retries — a turn waiting on another attempt is still ``queued`` or
    #    ``running``, and the caller parks the wakeup behind it instead of
    #    judging it.
    if ctx.turn_state == "failed":
        return GoalAction(
            kind="pause",
            reason="execution_failed",
            message=_clip(ctx.turn_detail),
        )
    if ctx.turn_state == "cancelled":
        return GoalAction(kind="pause", reason="cancelled")

    # 3. The turn has to be a round of *this* goal. A round created for an
    #    objective the owner has since replaced produced its report about that
    #    objective, and it cannot speak for this one.
    if ctx.turn_goal_id is not None and ctx.turn_goal_id != ctx.goal_id:
        return GoalAction(kind="none")

    # 4. A runtime that cannot deliver a report at all cannot run a goal: every
    #    round would be a round nobody could read the answer to.
    if not ctx.runtime_supported:
        return GoalAction(kind="pause", reason="runtime_unsupported")

    # 5. A report the platform could not read. Distinct from no report: a
    #    message that was never complete is not evidence that the model said
    #    nothing.
    if ctx.report_parse == "unavailable":
        return GoalAction(
            kind="pause",
            reason="report_unavailable",
            message=_clip(ctx.report_detail),
        )

    # 6. A malformed report gets one bounded chance to restate itself, and only
    #    on a runtime whose repair round can be trusted not to do work.
    if ctx.report_parse == "invalid":
        if ctx.repairs_used < MAX_REPORT_REPAIRS and ctx.repair_supported:
            return GoalAction(kind="queue_repair_round")
        return GoalAction(
            kind="pause",
            reason="report_invalid",
            message=_clip(
                f"the goal report could not be read ({ctx.report_detail})"
                if ctx.report_detail
                else "the goal report could not be read"
            ),
        )

    # 7. A person's input wins. Whatever this turn concluded, the owner has
    #    since said something, and the goal is judged again when that is done.
    if ctx.thread_has_queued_turn or ctx.thread_has_newer_input:
        return GoalAction(kind="none")

    # 8. An accepted terminal report. ``needs_input`` pauses and names what is
    #    missing; ``complete`` finishes the goal, and says on what basis.
    if ctx.report_parse == "valid" and ctx.report is not None:
        return _accept(ctx.report)

    # 9. The owner's own bound, checked *after* the report above, so a valid
    #    completion reported on the last permitted round still completes.
    #
    #    An unset bound stops nothing: this is the branch that used to end every
    #    goal at a default of ten rounds, and it now does nothing at all unless
    #    somebody explicitly asked for a number. ``rounds_used`` is read here and
    #    reported to the owner, but on its own it is not a reason to stop.
    if ctx.max_rounds is not None and ctx.rounds_used >= ctx.max_rounds:
        return GoalAction(
            kind="pause",
            reason="round_limit",
            message=(
                f"the goal reached the {ctx.max_rounds} automatic rounds you set "
                "for it; resume it to continue without that limit"
            ),
        )

    # 10. Ordinary continuation: the report was absent, or it was a repair
    #     round that legitimately came back with nothing to report.
    return GoalAction(kind="queue_work_round")


def _accept(report: GoalReport) -> GoalAction:
    """A valid report, turned into the state change it asks for."""
    if report.outcome == "needs_input":
        return GoalAction(kind="pause", reason="needs_input", message=_clip(report.reason))
    if report.outcome == "complete":
        # The name is the answer to "how does the platform know?", and it is the
        # only answer the first version has: the model said so.
        return GoalAction(kind="complete", completion_basis="model_reported")
    return GoalAction(kind="none")


def _clip(value: str | None) -> str | None:
    """A message bound to what the column accepts, without inventing one."""
    if not value:
        return None
    return value[:MAX_REASON_CHARS]


__all__ = ["GoalAction", "GoalActionKind", "GoalContext", "GoalReportOutcome", "decide"]
