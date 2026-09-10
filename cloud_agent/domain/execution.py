"""Job, attempt and event vocabulary shared by every layer.

These live here rather than beside the SQL that writes them. Routes, schemas,
tests and the host contract all speak them, and none of those should import a
repository to name a state.
"""

from __future__ import annotations

QUEUED = "queued"
WAITING = "waiting"
RUNNING = "running"
PUBLISHING = "publishing"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = (SUCCEEDED, FAILED, CANCELLED)


# The two event names the store writes itself, and the attempt statuses that
# fence every write an attempt can lose.
ATTEMPT_RUNNING = "running"
ATTEMPT_SUPERSEDED = "superseded"
ATTEMPT_FINISHED = "finished"
EVENT_ATTEMPT_SUPERSEDED = "attempt_superseded"
EVENT_ATTEMPT_ABORTED = "attempt_aborted"
ATTEMPT_ABORTED = "aborted"
