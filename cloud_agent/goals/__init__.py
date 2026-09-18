"""Conversation goals: an objective the platform keeps working toward.

A goal belongs to a conversation and is judged at the end of every turn. The
platform never decides whether the objective is met by itself — the working
model reports, and a completion claim buys exactly one extra verification turn
before the goal is called done. Every automatic round is an ordinary follow-up
turn, so it is cancellable, metered, navigable, and published like any other.
"""

from __future__ import annotations

from cloud_agent.goals.decision import GoalAction, GoalContext, decide
from cloud_agent.goals.models import (
    BLOCKED_STREAK,
    DEFAULT_ROUNDS,
    GOAL_BLOCKED_CODES,
    GOAL_PAUSE_REASONS,
    MAX_OBJECTIVE_CHARS,
    MAX_REASON_CHARS,
    MAX_ROUNDS,
    GoalBlockedCode,
    GoalConflict,
    GoalError,
    GoalInput,
    GoalLimits,
    GoalPauseReason,
    GoalReportStatus,
    GoalRoundKind,
    GoalState,
    GoalView,
)
from cloud_agent.goals.prompts import prompt_with_goal, render_round_prompt
from cloud_agent.goals.report import GoalReport, parse_goal_report

__all__ = [
    "BLOCKED_STREAK",
    "DEFAULT_ROUNDS",
    "GOAL_BLOCKED_CODES",
    "GOAL_PAUSE_REASONS",
    "MAX_OBJECTIVE_CHARS",
    "MAX_REASON_CHARS",
    "MAX_ROUNDS",
    "GoalAction",
    "GoalBlockedCode",
    "GoalConflict",
    "GoalContext",
    "GoalError",
    "GoalInput",
    "GoalLimits",
    "GoalPauseReason",
    "GoalReport",
    "GoalReportStatus",
    "GoalRoundKind",
    "GoalState",
    "GoalView",
    "decide",
    "parse_goal_report",
    "prompt_with_goal",
    "render_round_prompt",
]
