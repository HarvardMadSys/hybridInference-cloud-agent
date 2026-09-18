"""A conversation's completion goal, and the bounds the platform enforces on it.

The goal belongs to the *conversation* (``agent_threads``), not to a turn, and
it is single-valued: "no goal" is ``goal_objective IS NULL``. Every turn keeps
its own snapshot of what it was told and what it reported, so editing the
objective never rewrites history — the semantics ``agent_jobs.skills`` already
has.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic model field.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: An objective is a paragraph, not a document. It is carried verbatim in every
#: turn's prompt, so this bound is also what keeps that prefix bounded.
MAX_OBJECTIVE_CHARS = 4000
#: One line of evidence, or what still remains. Truncated rather than refused:
#: a model that wrote three paragraphs is not wrong about *what* to say.
MAX_REASON_CHARS = 2000
#: A goal that cannot finish in fifty automatic rounds is not a task, it is a
#: subscription. The owner's account quota is what bounds spend; this bounds
#: how long the platform keeps spending it without being asked again.
MAX_ROUNDS = 50
DEFAULT_ROUNDS = 10
#: Consecutive turns that must report ``blocked`` before the platform accepts
#: it. One model saying "I cannot" is not evidence; three turns of trying is.
BLOCKED_STREAK = 3
#: How long an ``active`` goal may sit with no queued or running turn before the
#: reconciler pauses it. This is a backstop for a path that forgot to pause, not
#: a mechanism: every ordinary way a goal stops being able to continue pauses it
#: where and when it happens.
STALLED_AFTER_S = 900.0

GoalState = Literal["active", "paused", "blocked", "complete"]
GoalRoundKind = Literal["work", "verify"]
GoalReportStatus = Literal["continue", "complete", "blocked"]

#: Why an ``active`` goal stopped continuing by itself. ``user`` and ``fork``
#: are the two an owner chooses (pause, and forking a conversation); the rest
#: are the platform telling the owner that a turn did not end workably.
GOAL_PAUSE_REASONS = (
    "user",
    "fork",
    "turn-failed",
    "cancelled",
    "context-lost",
    "archived",
    "stalled",
)
GoalPauseReason = Literal[
    "user", "fork", "turn-failed", "cancelled", "context-lost", "archived", "stalled"
]
#: ``round-limit`` is the platform refusing to keep going; ``model-reported``
#: is the model's own claim, accepted only after ``BLOCKED_STREAK`` turns.
GOAL_BLOCKED_CODES = ("round-limit", "model-reported")
GoalBlockedCode = Literal["round-limit", "model-reported"]


class GoalLimits(BaseModel):
    """Advertise the server's goal bounds to clients before they submit."""

    max_objective_chars: int = MAX_OBJECTIVE_CHARS
    max_rounds: int = MAX_ROUNDS
    default_rounds: int = DEFAULT_ROUNDS


class GoalError(ValueError):
    """A goal operation the caller got wrong and can correct."""


class GoalConflict(GoalError):
    """A write made against a revision the goal has already moved past."""


class GoalInput(BaseModel):
    """An owner's completion condition for one conversation."""

    model_config = ConfigDict(str_strip_whitespace=True)

    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    max_rounds: int = Field(DEFAULT_ROUNDS, ge=1, le=MAX_ROUNDS)


class GoalView(BaseModel):
    """The conversation's goal as its owner reads it."""

    objective: str
    state: GoalState
    revision: int
    max_rounds: int
    rounds_used: int
    blocked_streak: int = 0
    pause_reason: GoalPauseReason | None = None
    blocked_code: GoalBlockedCode | None = None
    blocked_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
