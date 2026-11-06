"""A conversation's completion goal, and the bounds the platform enforces on it.

The goal belongs to the *conversation* (``agent_threads``), not to a turn, and
it is single-valued: "no goal" is ``goal_objective IS NULL``. Every turn keeps
its own snapshot of what it was told and what it reported, so editing the
objective never rewrites history — the semantics ``agent_jobs.skills`` already
has.

Three durable states, and only three: ``active``, ``paused`` and ``complete``.
*Why* a goal stopped is data, not a state — ``pause_reason`` carries it — which
is what lets "the owner paused this", "the model says it needs input" and "the
owner's own round cap was reached" share one lifecycle and one resume path
instead of three.

**A goal has no round limit by default.** The product asks the owner for one
thing — how far they want the work taken — and then keeps going until the
objective is met, the owner stops it, the platform hits a condition that needs
the owner, or one of the run-time limits that already governs every turn
(account quota, a single execution's deadline, cancellation) applies. There is
no default budget and no maximum; a goal is not a subscription that expires, and
"999999 rounds" is not how a design says "no limit".

``max_rounds`` survives for one reason only: an owner may still state a bound
explicitly, and a goal that already carries one must not silently lose it. When
it is set it is enforced, and it is *distinguishable* from the absence of one —
:data:`UNLIMITED_ROUNDS` is ``None``, never a sentinel integer. ``rounds_used``
is a statistic the owner reads, not a stop condition.

``revision`` is the thread-level control version: it moves on every accepted
goal write, including ``clear``, and never resets. ``goal_id`` is the *identity*
of the objective being worked toward: replacing the objective mints a new one,
so a report carrying the old identity can be told apart from a report about the
new one even when the text is identical.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic model field.
from typing import Literal

from pydantic import BaseModel

from cloud_agent.domain.goals import GoalPauseReason  # noqa: TC001 - Pydantic model field.
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
#: What "no round limit" is spelled as, everywhere: the absence of a value.
#:
#: Deliberately not a large integer. A sentinel is indistinguishable from a real
#: bound in the column, in an API response and in a bug report, and it forces
#: every reader to know the magic number — after which "raise the cap" and
#: "remove the cap" look like the same operation. ``None`` says what it means and
#: cannot be confused with a number an owner chose.
UNLIMITED_ROUNDS: None = None
#: A turn whose report block is present but malformed gets one bounded chance to
#: restate it. Only when the runtime can be pinned to tool-free execution, and
#: only once: a second failure pauses.
MAX_REPORT_REPAIRS = 1

GoalState = Literal["active", "paused", "complete"]
#: ``work`` is an ordinary automatic round. ``repair`` is the bounded second
#: chance a malformed report gets; it is still a model round and it is still
#: charged to the budget.
GoalRoundKind = Literal["work", "repair"]

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
    """Advertise the server's goal bounds to clients before they submit.

    Only the objective is bounded. There is no advertised round ceiling and no
    advertised default, because there is neither: a client that wants a bound
    states one, and a client that does not leaves the field out. Advertising a
    number here is what made "unlimited" look like "fifty" to every reader.
    """

    max_objective_chars: int = MAX_OBJECTIVE_CHARS


class GoalError(ValueError):
    """A goal operation the caller got wrong and can correct."""


class GoalConflict(GoalError):
    """A write made against a revision the goal has already moved past."""


class GoalView(BaseModel):
    """The conversation's goal as its owner reads it."""

    goal_id: str
    objective: str
    state: GoalState
    revision: int
    #: ``None`` is "no limit was set", which is the ordinary case. It is never a
    #: large number: a reader must be able to tell "the owner bounded this" from
    #: "nothing bounds this" without knowing a convention.
    max_rounds: int | None = None
    #: How many automatic rounds have actually been charged. A statistic the
    #: owner reads, not a stop condition.
    rounds_used: int
    pause_reason: GoalPauseReason | None = None
    pause_detail: str | None = None
    completion_basis: CompletionBasis | None = None
    completed_by_job_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "GOAL_REPORT_PROTOCOL",
    "GOAL_REPORT_RUNTIMES",
    "MAX_OBJECTIVE_CHARS",
    "MAX_REASON_CHARS",
    "MAX_REPORT_REPAIRS",
    "REPORT_REPAIR_RUNTIMES",
    "UNLIMITED_ROUNDS",
    "CompletionBasis",
    "GoalConflict",
    "GoalError",
    "GoalLimits",
    "GoalRoundKind",
    "GoalState",
    "GoalView",
    "GoalWakeupState",
    "GoalWakeupTrigger",
]
