"""What a failed attempt at an administrator's sandbox deletion leads to.

The deletion worker reports a failure, and the store records it under the
request's claim. Whether the request is tried again, when, and what the
administrator reads is decided here, from what the locked row says, so the
store only locks, reads and writes.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Automatic attempts one deletion request may spend before an administrator
#: has to resume it.
DELETION_RETRY_MAX_ATTEMPTS = 8
#: How long after a request (or its resumption) automatic retries continue.
DELETION_RETRY_WINDOW_SECONDS = 24 * 60 * 60
#: The first retry's delay, doubled for every attempt after it, up to the cap.
DELETION_RETRY_BASE_SECONDS = 60
DELETION_RETRY_MAX_SECONDS = 60 * 60

#: The retry classes a worker may name. Anything else is treated as needing
#: an administrator.
RETRY_CLASSES = frozenset({"transient", "permanent", "operator_action"})

_TERMINAL_REASONS = {
    "permanent": "permanent_failure",
    "operator_action": "operator_action_required",
    "transient": "retry_not_retained",
}


@dataclass(frozen=True, slots=True)
class DeletionFailure:
    """A fenced deletion request's state and the failure reported against it."""

    #: ``pending`` or ``archived``: the request's deletion state when it failed.
    deletion_state: str
    #: Attempts the request has spent, the one that failed included.
    retry_count: int
    #: Whether the request's retry window is still open, by the database clock.
    window_open: bool
    #: Whether the worker asks for a pending request to stay pending.
    retain_pending: bool
    #: The retry class the worker reported, if any.
    retry_class: str | None
    #: The worker's error text.
    error: str


@dataclass(frozen=True, slots=True)
class DeletionDecision:
    """What the store writes for one recorded failure."""

    #: The reported class, or ``operator_action`` for one this does not know.
    retry_class: str
    retry_scheduled: bool
    #: Automatic retries were allowed but the budget or window ran out.
    retry_exhausted: bool
    #: The request keeps its state, and stops retrying until resumed.
    retry_stopped: bool
    #: Seconds until the scheduled retry; ``None`` when none is scheduled.
    delay_seconds: float | None
    #: The error stored for, and shown to, the administrator.
    error: str
    #: Why automatic handling ended, or ``None`` while a retry is scheduled.
    terminal_reason: str | None


def decide_deletion_failure(failure: DeletionFailure) -> DeletionDecision:
    """Decide whether a failed deletion is retried, and what is recorded.

    An archived request, or a pending one the worker asks to keep, is retried
    automatically when its failure is ``transient`` and the budget and window
    allow. Otherwise it keeps its state with automatic retries stopped, for an
    administrator to resume; any other request becomes ``failed``.
    """
    retry_requested = failure.deletion_state == "archived" or failure.retain_pending
    retry_class = failure.retry_class if failure.retry_class in RETRY_CLASSES else "operator_action"
    automatic_retry_allowed = retry_class == "transient"
    retry_scheduled = (
        retry_requested
        and automatic_retry_allowed
        and failure.retry_count < DELETION_RETRY_MAX_ATTEMPTS
        and failure.window_open
    )
    retry_exhausted = retry_requested and automatic_retry_allowed and not retry_scheduled
    retry_stopped = retry_requested and not retry_scheduled
    error = failure.error
    delay_seconds: float | None = None
    if retry_scheduled:
        delay_seconds = min(
            DELETION_RETRY_BASE_SECONDS * (2 ** max(failure.retry_count - 1, 0)),
            DELETION_RETRY_MAX_SECONDS,
        )
        final_error = error[:400]
    elif retry_exhausted:
        final_error = (
            error[:300] + " Automatic deletion retry budget exhausted; an administrator can resume "
            "this request."
        )
    elif retry_stopped:
        final_error = (
            error[:300]
            + " Automatic retries stopped for this failure; an administrator can resume "
            "this request after resolving it."
        )
    else:
        final_error = error[:500]
    terminal_reason = None
    if retry_exhausted:
        terminal_reason = "retry_budget_exhausted"
    elif not retry_scheduled:
        terminal_reason = _TERMINAL_REASONS[retry_class]
    return DeletionDecision(
        retry_class=retry_class,
        retry_scheduled=retry_scheduled,
        retry_exhausted=retry_exhausted,
        retry_stopped=retry_stopped,
        delay_seconds=delay_seconds,
        error=final_error,
        terminal_reason=terminal_reason,
    )


__all__ = [
    "DELETION_RETRY_BASE_SECONDS",
    "DELETION_RETRY_MAX_ATTEMPTS",
    "DELETION_RETRY_MAX_SECONDS",
    "DELETION_RETRY_WINDOW_SECONDS",
    "RETRY_CLASSES",
    "DeletionDecision",
    "DeletionFailure",
    "decide_deletion_failure",
]
