"""A conversation's completion goal, and the bounds the platform enforces on it.

The goal belongs to the *conversation* (``agent_threads``), not to a turn, and
it is single-valued: "no goal" is ``goal_objective IS NULL``. Every turn keeps
its own snapshot of what it was told and what it reported, so editing the
objective never rewrites history — the semantics ``agent_jobs.skills`` already
has.

Three durable states, and only three: ``active``, ``paused`` and ``complete``.
*Why* a goal stopped is data, not a state — ``pause_reason`` carries it — which
is what lets "the owner paused this", "the model says it needs input" and "the
platform ran out of rounds" share one lifecycle and one resume path instead of
three.

``revision`` is the thread-level control version: it moves on every accepted
goal write, including ``clear``, and never resets. ``goal_id`` is the *identity*
of the objective being worked toward: replacing the objective mints a new one,
so a report carrying the old identity can be told apart from a report about the
new one even when the text is identical.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic model field.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent_shared.shared_protocol import GOAL_REPORT_CAPABILITY

#: An objective is a paragraph, not a document. It is carried verbatim in every
#: turn's prompt, so this bound is also what keeps that prefix bounded.
MAX_OBJECTIVE_CHARS = 4000
#: One line of evidence, or what still remains. Truncated rather than refused:
#: a model that wrote three paragraphs is not wrong about *what* to say.
MAX_REASON_CHARS = 2000
#: The report protocol a host must be able to deliver for a goal round to be
#: admitted to it. A runtime that cannot identify its final message reliably
#: enough to extract the block cannot run automatic rounds at all — it would be
#: a loop that never reads an answer.
#:
#: The literal lives in the shared contracts module, because the *host* is what
#: advertises it and the two images must agree on the string. Importing it here
#: rather than restating it is what stops the declaration and the check from
#: drifting apart in a rolling deploy.
GOAL_REPORT_PROTOCOL = GOAL_REPORT_CAPABILITY
#: The runtimes whose final message the platform can attribute well enough to
#: read a report out of it. Verified per pinned version, not assumed from the
#: runtime's name: a runtime missing from this set does not silently loop, it
#: gets a goal paused with ``runtime_unsupported``.
GOAL_REPORT_RUNTIMES = frozenset({"claude-code", "codex", "kilo", "opencode", "pi"})
#: Runtimes that can additionally be pinned to a *tool-free* repair round, which
#: is the only condition under which a malformed report is allowed to cost a
#: second model round. Empty on purpose: no runtime has passed that verification
#: yet, so a malformed report pauses with ``report_invalid`` and the owner is
#: told which format was wrong. Adding a name here is a claim that has to be
#: earned against a real pinned version, and the design says so.
REPORT_REPAIR_RUNTIMES: frozenset[str] = frozenset()
#: A goal that cannot finish in fifty automatic rounds is not a task, it is a
#: subscription. The owner's account quota is what bounds spend; this bounds
#: how long the platform keeps spending it without being asked again.
MAX_ROUNDS = 50
DEFAULT_ROUNDS = 10
#: A turn whose report block is present but malformed gets one bounded chance to
#: restate it. Only when the runtime can be pinned to tool-free execution, and
#: only once: a second failure pauses.
MAX_REPORT_REPAIRS = 1
#: How recently an ``active`` goal with no pending obligation must have moved
#: for the recovery sweep to assume a consumer is still about to act on it.
#: Long enough to cover a decision in flight, short enough that a goal nothing
#: is working on is noticed rather than re-armed for ever.
ARM_AFTER_S = 60.0
#: How long an ``active`` goal may sit with no queued or running turn and no
#: pending wakeup before the recovery sweep pauses it. This is a backstop for a
#: path that forgot to schedule, not a mechanism: every ordinary way a goal
#: stops being able to continue pauses it where and when it happens.
STALLED_AFTER_S = 900.0
#: How many times a wakeup may fail on temporary scheduling infrastructure
#: before the goal is paused with ``scheduling_failed`` rather than retried
#: forever behind a green "active" badge.
MAX_WAKEUP_ATTEMPTS = 5
#: The first backoff step; each retry doubles it up to the cap.
WAKEUP_BACKOFF_S = 5.0
WAKEUP_BACKOFF_MAX_S = 300.0

GoalState = Literal["active", "paused", "complete"]
#: ``work`` is an ordinary automatic round. ``repair`` is the bounded second
#: chance a malformed report gets; it is still a model round and it is still
#: charged to the budget.
GoalRoundKind = Literal["work", "repair"]

#: Why an ``active`` goal stopped continuing by itself. The first two are what
#: an owner asks for; the rest are the platform telling the owner which
#: condition stopped it. ``fork`` is recorded on the *child* task, never on the
#: parent, because a fork copies an objective without inheriting the authority
#: to run it.
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

#: The only way a first-version goal completes: the model said so, in a valid
#: report, on a turn that ended workably. The platform does not claim to have
#: independently verified the objective, and the name says so.
CompletionBasis = Literal["model_reported"]

#: What triggers a scheduling obligation. ``owner`` covers create, edit and
#: resume — anything an owner did that should make the goal advance. ``finish``
#: is a turn reaching a terminal state.
GoalWakeupTrigger = Literal["owner", "finish"]
GoalWakeupState = Literal["pending", "processed"]


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

    goal_id: str
    objective: str
    state: GoalState
    revision: int
    max_rounds: int
    rounds_used: int
    pause_reason: GoalPauseReason | None = None
    pause_detail: str | None = None
    completion_basis: CompletionBasis | None = None
    completed_by_job_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GoalSummary(BaseModel):
    """The control version of a conversation that has no goal.

    ``GET`` on a conversation whose goal was cleared still has to answer with
    the version it is at, because the next write's ``expected_revision`` is
    compared against it: a client that read "no goal" and then created one must
    be able to prove nothing was created in between.
    """

    goal: GoalView | None = None
    revision: int
    max_rounds: int = MAX_ROUNDS
