"""Job, attempt and event lifecycle values.

The names a row can carry, in one place. The store writes them, the API reads
them, and neither owns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class JobStreamState:
    """The current turn state and pull-request URL an event stream needs."""

    state: str
    published_pr_url: str | None


# Job lifecycle. ``cancel_requested`` is a flag orthogonal to ``state``: it is
# delivered to the running worker via the heartbeat response, and the worker
# performs the fenced transition to ``cancelled``.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = (SUCCEEDED, FAILED, CANCELLED)

#: The state a new turn is created in. A Fork's copies of its source's turns
#: are created already settled, in the terminal state each source turn reached.
INITIAL_STATE = QUEUED

#: For each state, the states the store's writes can move a turn to from it.
#: It is what the statements do today: ``tests/unit/test_job_state_transitions.py``
#: names every statement that writes ``agent_jobs.state`` and requires this
#: table to be exactly the union of what they can do. ``waiting`` is not here:
#: ``0005_queued_follow_ups`` retired it, and the schema no longer accepts it.
JOB_STATE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, FAILED, CANCELLED}),
    RUNNING: frozenset({QUEUED, SUCCEEDED, FAILED, CANCELLED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}


def job_state_change_allowed(old: str, new: str) -> bool:
    """Whether a write that moves a turn from ``old`` to ``new`` is one the store makes."""
    return old == new or new in JOB_STATE_TRANSITIONS.get(old, frozenset())


# Attempt lifecycle.
ATTEMPT_RUNNING = "running"
ATTEMPT_SUPERSEDED = "superseded"
ATTEMPT_FINISHED = "finished"

# Control event appended by the reaper when it supersedes an attempt.
EVENT_ATTEMPT_SUPERSEDED = "attempt_superseded"
EVENT_ATTEMPT_ABORTED = "attempt_aborted"

# An attempt the platform gave up on before the agent ever started -- the job
# never got its turn, so it must not count toward the retry budget.
ATTEMPT_ABORTED = "aborted"
