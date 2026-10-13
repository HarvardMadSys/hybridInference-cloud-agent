"""The lifecycle of a call that needed a decision, and its timeout contract.

A held call is the one place in this feature where the platform stops and waits
for a person, which means it is the one place where a timeout can be wrong in a
way nobody notices. Four clocks run at once -- how long the owner is given to
decide, how long the CLI will wait for a tool result, how long the relay will
hold a request, and when the job's lease expires -- and a platform that applies
its own clock alone ends up either abandoning approvals the owner was still
considering or holding a lease open for a job that is already gone.

The contract is therefore one value: :func:`deadline_for` returns the earliest
instant any layer is willing to wait until, and everything else derives from
it. Waiting never holds a database transaction, and the job keeps its normal
lease while it waits, so a job waiting on its owner is not distinguishable from
a job doing anything else.

Transitions are a pure function of the current state and an event, so the
interesting cases are testable without a clock or a database. Two of them are
load-bearing:

**A decision that arrives late is not a decision.** Approving a call that
expired, or that was already cancelled by the runtime, must not revive it; the
owner is approving something they were shown, and what they were shown is
gone.

**Dispatch is claimed atomically, and never retried blindly.** The transition
into ``dispatching`` is where exactly one executor wins. A caller that finds
the row already dispatching does *not* send it again -- an external side effect
may have already happened -- and a caller that loses the response records
``outcome_unknown`` rather than guessing. Tool annotations never make a retry
safe; only a contract the server itself documents could, and none does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from cloud_agent.mcp.models import Invocation, InvocationStatus

#: What may happen to a held call.
Event = Literal[
    "approve",
    "deny",
    "timeout",
    "cancel",
    "claim",
    "dispatch_ok",
    "dispatch_error",
    "dispatch_lost",
]

#: How long an owner is given to answer, when nothing tighter applies.
DEFAULT_DECISION_WINDOW = timedelta(minutes=10)

#: The largest stored argument body a held call may carry, in encoded bytes.
#: A held call keeps its arguments, encrypted, in a table every tenant shares
#: until its owner decides, so that the owner can see what they are approving.
#: A body past this is not something a person reviews, and it is refused
#: rather than stored.
MAX_HELD_ARGUMENTS_BYTES = 256 * 1024

#: Transition table. A pair absent from it is refused rather than ignored,
#: because silently accepting "deny after completion" would rewrite history.
_TRANSITIONS: dict[tuple[InvocationStatus, Event], InvocationStatus] = {
    (InvocationStatus.PENDING, "approve"): InvocationStatus.APPROVED,
    (InvocationStatus.PENDING, "deny"): InvocationStatus.DENIED,
    (InvocationStatus.PENDING, "timeout"): InvocationStatus.EXPIRED,
    (InvocationStatus.PENDING, "cancel"): InvocationStatus.EXPIRED,
    (InvocationStatus.APPROVED, "claim"): InvocationStatus.DISPATCHING,
    (InvocationStatus.APPROVED, "cancel"): InvocationStatus.EXPIRED,
    (InvocationStatus.APPROVED, "timeout"): InvocationStatus.EXPIRED,
    (InvocationStatus.DISPATCHING, "dispatch_ok"): InvocationStatus.COMPLETED,
    (InvocationStatus.DISPATCHING, "dispatch_error"): InvocationStatus.FAILED,
    (InvocationStatus.DISPATCHING, "dispatch_lost"): InvocationStatus.OUTCOME_UNKNOWN,
}

#: States from which nothing further can happen.
TERMINAL_STATUSES: frozenset[InvocationStatus] = frozenset(
    {
        InvocationStatus.DENIED,
        InvocationStatus.EXPIRED,
        InvocationStatus.COMPLETED,
        InvocationStatus.FAILED,
        InvocationStatus.OUTCOME_UNKNOWN,
    }
)


class TransitionRefused(RuntimeError):
    """Raised when an event cannot apply to a call in its current state."""

    def __init__(self, status: InvocationStatus, event: Event) -> None:
        """Describe the refused pair."""
        super().__init__(f"cannot apply {event!r} to a call that is {status.value}")
        self.status = status
        self.event = event


def transition(status: InvocationStatus, event: Event) -> InvocationStatus:
    """Apply one event, refusing a pair the table does not define.

    Refusing rather than clamping is what makes the late-decision case safe: a
    caller that tries to approve an expired call gets an error it must handle,
    not a silent resurrection.
    """
    following = _TRANSITIONS.get((status, event))
    if following is None:
        raise TransitionRefused(status, event)
    return following


def can_transition(status: InvocationStatus, event: Event) -> bool:
    """Whether an event applies, for callers that are asking rather than acting."""
    return (status, event) in _TRANSITIONS


@dataclass(frozen=True)
class Deadline:
    """The earliest instant any layer is willing to wait until, and which one."""

    expires_at: datetime
    source: str

    def remaining(self, *, now: datetime | None = None) -> timedelta:
        """How long is left, never negative."""
        current = now or datetime.now(timezone.utc)
        return max(self.expires_at - current, timedelta(0))

    @property
    def passed(self) -> bool:
        """Whether the deadline is already behind us."""
        return self.remaining() == timedelta(0)


def deadline_for(
    *,
    created_at: datetime,
    decision_window: timedelta = DEFAULT_DECISION_WINDOW,
    cli_timeout: timedelta | None = None,
    relay_timeout: timedelta | None = None,
    runner_deadline: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> Deadline:
    """The earliest deadline among every layer that is waiting.

    Each layer is optional because each is absent in some configuration -- a
    job with no lease deadline, a runtime that reports no tool timeout -- and an
    absent layer must not become an infinite one by accident. Only the decision
    window is required, so there is always an answer.
    """
    candidates: list[tuple[datetime, str]] = [
        (_ensure_aware(created_at) + decision_window, "decision_window")
    ]
    if cli_timeout is not None:
        candidates.append((_ensure_aware(created_at) + cli_timeout, "cli_timeout"))
    if relay_timeout is not None:
        candidates.append((_ensure_aware(created_at) + relay_timeout, "relay_timeout"))
    if runner_deadline is not None:
        candidates.append((_ensure_aware(runner_deadline), "runner_deadline"))
    if lease_expires_at is not None:
        candidates.append((_ensure_aware(lease_expires_at), "lease"))
    expires_at, source = min(candidates, key=lambda item: item[0])
    return Deadline(expires_at=expires_at, source=source)


def _ensure_aware(value: datetime) -> datetime:
    """Read a naive timestamp as UTC, matching how the store writes them."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class DispatchFence:
    """The versions a dispatcher must still see before it may send a call."""

    config_revision: int
    authorization_epoch: int
    capability_revision: int
    policy_revision: int
    lease_generation: int
    dispatch_generation: int


def fence_matches(invocation: Invocation, current: DispatchFence) -> bool:
    """Whether a call may still be dispatched under the versions now in force.

    Total and one-directional: every version the approval was made under must
    still hold. A narrowing that happens while a call waits is exactly the case
    this exists for -- the owner revoked the tool, and the approval that was
    already granted must not survive it.
    """
    return (
        invocation.config_revision == current.config_revision
        and invocation.authorization_epoch == current.authorization_epoch
        and invocation.capability_revision == current.capability_revision
        and invocation.policy_revision == current.policy_revision
        and invocation.lease_generation == current.lease_generation
    )


def is_retry_safe(invocation: Invocation) -> bool:
    """Whether a failed dispatch may be retried automatically.

    Always false, and the function exists to say so in one place. A tool
    annotation claiming idempotency is the server's own description of itself,
    and the specification is explicit that annotations are untrusted; the
    alternative -- retrying a write because the response was lost -- is how a
    platform files the same issue twice. Recovery is a decision for the person
    who can see the external system.
    """
    del invocation
    return False


RECOVERY_GUIDANCE = (
    "This call was sent but the result could not be confirmed, so it will not be retried. "
    "Check the service directly before asking the agent to try again."
)


def recovery_note(invocation: Invocation) -> str:
    """What the interface tells the owner about an unconfirmable call."""
    if invocation.status is InvocationStatus.OUTCOME_UNKNOWN:
        return RECOVERY_GUIDANCE
    if invocation.status is InvocationStatus.EXPIRED:
        return "This approval expired before it was used. Ask the agent again to re-request it."
    if invocation.status is InvocationStatus.DENIED:
        return "This call was denied."
    return ""


__all__ = [
    "DEFAULT_DECISION_WINDOW",
    "MAX_HELD_ARGUMENTS_BYTES",
    "RECOVERY_GUIDANCE",
    "TERMINAL_STATUSES",
    "Deadline",
    "DispatchFence",
    "Event",
    "TransitionRefused",
    "can_transition",
    "deadline_for",
    "fence_matches",
    "is_retry_safe",
    "recovery_note",
    "transition",
]
