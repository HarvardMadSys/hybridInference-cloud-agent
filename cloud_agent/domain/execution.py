"""Job, attempt and event lifecycle values.

The names a row can carry, in one place. The store writes them, the API reads
them, and neither owns them.
"""

from __future__ import annotations

# Job lifecycle. ``cancel_requested`` is a flag orthogonal to ``state``: it is
# delivered to the running worker via the heartbeat response, and the worker
# performs the fenced transition to ``cancelled``.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = (SUCCEEDED, FAILED, CANCELLED)

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
