"""Goal vocabulary: why a goal paused, what a turn reported, what happens next.

Pure values the goal flow, its ports and its storage all name. The rules that
produce them stay in :mod:`cloud_agent.goals`: :mod:`~cloud_agent.goals.report`
parses a final message into these, and :mod:`~cloud_agent.goals.decision`
decides a :class:`GoalAction` from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Why an ``active`` goal stopped continuing by itself. The first two are what
#: an owner asks for; the rest are the platform telling the owner which
#: condition stopped it. ``fork`` is recorded on the *child* task, never on the
#: parent, because a fork copies an objective without inheriting the authority
#: to run it.
#:
#: ``round_limit`` is not a default stop any more — it is reachable only when the
#: owner stated a bound themselves, and it stays in the vocabulary because rows
#: written before that change still carry it. Resuming one releases the spent
#: bound, so the state is never a dead end.
GOAL_PAUSE_REASONS = (
    "user",
    "fork",
    "needs_input",
    "round_limit",
    "execution_failed",
    "cancelled",
    "report_invalid",
    "report_unavailable",
    "runtime_unsupported",
    "context_lost",
    "access_denied",
    "quota_unavailable",
    "scheduling_failed",
    "archived",
)
GoalPauseReason = Literal[
    "user",
    "fork",
    "needs_input",
    "round_limit",
    "execution_failed",
    "cancelled",
    "report_invalid",
    "report_unavailable",
    "runtime_unsupported",
    "context_lost",
    "access_denied",
    "quota_unavailable",
    "scheduling_failed",
    "archived",
]

GoalReportOutcome = Literal["complete", "needs_input"]
#: ``absent`` and ``valid`` are the two answers the platform acts on directly.
#: ``invalid`` earns one repair round; ``unavailable`` pauses, because a report
#: that could not be read is not the same fact as a report that was not made.
GoalReportParse = Literal["absent", "valid", "invalid", "unavailable"]


@dataclass(frozen=True)
class GoalReport:
    """What one turn said about the goal, and why."""

    outcome: GoalReportOutcome
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedGoalReport:
    """The outcome of reading one final message, with a reason when it failed."""

    result: GoalReportParse
    report: GoalReport | None = None
    #: Machine-readable, one of
    #: :data:`cloud_agent.goals.report.REPORT_INVALID_REASONS`. Only set when
    #: ``result`` is ``invalid``.
    detail: str | None = None


@dataclass(frozen=True)
class GoalReportEnvelope:
    """One turn's answer about its goal, as the control plane stores it.

    The same four facts the runtime adapter produces, plus the provenance of the
    message they were read out of. It crosses one boundary — the finish request —
    and everything on the far side reads it as *what the model said*, never as
    something the platform verified. The identity fields (job, attempt, lease,
    goal, revision) are deliberately absent: they are bound by the authenticated
    execution context, and a model's own claim about which goal it was working
    on is not evidence about anything.
    """

    parse_result: GoalReportParse
    outcome: GoalReportOutcome | None = None
    reason: str | None = None
    #: A list, not a tuple: this is the value that goes into a JSONB column, and
    #: the driver will not encode a tuple into one.
    evidence: list[str] | None = None
    detail: str | None = None
    #: Which final message this was read from. The audit trail points at it; the
    #: report's content is not republished here.
    source_digest: str | None = None

    @classmethod
    def from_parsed(
        cls, parsed: ParsedGoalReport, *, source_digest: str | None
    ) -> GoalReportEnvelope:
        """The envelope for one parsed final message."""
        report = parsed.report
        return cls(
            parse_result=parsed.result,
            outcome=report.outcome if report is not None else None,
            reason=report.reason if report is not None else None,
            evidence=list(report.evidence) if report is not None else None,
            detail=parsed.detail,
            source_digest=source_digest,
        )


#: What the platform does next. ``none`` covers every state that is not
#: ``active``, every case where another turn already owns the outcome, and
#: "yield to the message a person already queued".
GoalActionKind = Literal["none", "queue_work_round", "queue_repair_round", "complete", "pause"]


@dataclass(frozen=True)
class GoalAction:
    """The one thing to persist, and the counters that go with it."""

    kind: GoalActionKind
    reason: GoalPauseReason | None = None
    message: str | None = None
    #: ``complete`` only: the name of the evidence the platform accepted.
    completion_basis: str | None = None
