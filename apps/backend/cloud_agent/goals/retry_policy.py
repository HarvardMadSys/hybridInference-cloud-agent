"""Scheduling retry limits and delay for a conversation's goal.

These rules decide when the controller retries or pauses a goal. They read the
current settings each time, so an administrator can change the policy without
restarting workers. Persistence records the resulting delay rather than owning
the decision.
"""

from __future__ import annotations

import math

from cloud_agent.goals.models import (
    MAX_WAKEUP_ATTEMPTS,
    WAKEUP_BACKOFF_MAX_S,
    WAKEUP_BACKOFF_S,
)
from cloud_agent.settings import integer_setting, number_setting


def wakeup_backoff(attempts: int) -> float:
    """How long to wait before trying a failed wakeup again.

    Doubling from a few seconds to the cap. The point is to stop a hot loop over
    a database that is refusing writes, not to schedule anything precisely:
    every wakeup that matters is also re-armed by the finish that unblocks it.
    """
    backoff = number_setting("goals.wakeup_backoff_seconds", WAKEUP_BACKOFF_S)
    max_backoff = number_setting("goals.wakeup_backoff_max_seconds", WAKEUP_BACKOFF_MAX_S)
    if attempts <= 1:
        return min(backoff, max_backoff)
    return min(backoff * math.pow(2, min(attempts - 1, 100)), max_backoff)


def is_exhausted(attempts: int) -> bool:
    """Whether a wakeup has failed often enough to stop retrying it."""
    return attempts >= integer_setting("goals.max_wakeup_attempts", MAX_WAKEUP_ATTEMPTS)


__all__ = ["is_exhausted", "wakeup_backoff"]
