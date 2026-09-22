"""Conversation goals: an objective the platform keeps working toward.

A goal belongs to a conversation and is judged at the end of every automatic
round. The platform never decides whether the objective is met by itself: the
working model reports, in a strict block at the end of its final message, and
the platform checks that the report is readable, belongs to the current
objective, and came from a turn that ended workably. A completion is therefore
``model_reported`` — real, and named for exactly what it is.

Every automatic round is an ordinary queued turn: same lease, same fence, same
metering, same cancellation, same publishing. The platform schedules them
through durable wakeups, so a crash between "this turn ended" and "the next turn
exists" cannot lose a goal.
"""

from __future__ import annotations

from cloud_agent.goals.decision import GoalAction, GoalContext, decide
from cloud_agent.goals.models import (
    GOAL_PAUSE_REASONS,
    GOAL_REPORT_PROTOCOL,
    GOAL_REPORT_RUNTIMES,
    MAX_OBJECTIVE_CHARS,
    MAX_REASON_CHARS,
    MAX_REPORT_REPAIRS,
    REPORT_REPAIR_RUNTIMES,
    STALLED_AFTER_S,
    UNLIMITED_ROUNDS,
    GoalConflict,
    GoalError,
    GoalInput,
    GoalLimits,
    GoalPauseReason,
    GoalRoundKind,
    GoalState,
    GoalView,
    GoalWakeupTrigger,
)
from cloud_agent.goals.prompts import (
    REPORT_INSTRUCTIONS,
    prompt_with_goal,
    render_repair_prompt,
    render_round_prompt,
)
from cloud_agent.goals.report import (
    GOAL_REPORT_FENCE,
    REPORT_INVALID_REASONS,
    REPORT_VERSION,
    GoalReport,
    GoalReportEnvelope,
    GoalReportOutcome,
    GoalReportParse,
    ParsedGoalReport,
    parse_goal_report,
)

__all__ = [
    "GOAL_PAUSE_REASONS",
    "GOAL_REPORT_FENCE",
    "GOAL_REPORT_PROTOCOL",
    "GOAL_REPORT_RUNTIMES",
    "MAX_OBJECTIVE_CHARS",
    "MAX_REASON_CHARS",
    "MAX_REPORT_REPAIRS",
    "REPORT_INSTRUCTIONS",
    "REPORT_INVALID_REASONS",
    "REPORT_REPAIR_RUNTIMES",
    "REPORT_VERSION",
    "STALLED_AFTER_S",
    "UNLIMITED_ROUNDS",
    "GoalAction",
    "GoalConflict",
    "GoalContext",
    "GoalError",
    "GoalInput",
    "GoalLimits",
    "GoalPauseReason",
    "GoalReport",
    "GoalReportEnvelope",
    "GoalReportOutcome",
    "GoalReportParse",
    "GoalRoundKind",
    "GoalState",
    "GoalView",
    "GoalWakeupTrigger",
    "ParsedGoalReport",
    "decide",
    "parse_goal_report",
    "prompt_with_goal",
    "render_repair_prompt",
    "render_round_prompt",
]
