"""Retention for the host-capacity history the Servers chart reads.

**The points themselves are not written here.** They are written by the
heartbeat that accepts an observation, inside that request's own transaction.
That is not an optimization but the only place the write is correct: the host
row holds one observation and every heartbeat replaces it, so anything that
re-read that row on a clock of its own would see whichever sample happened to
be there at that instant and would never learn about the ones in between.

This module used to be that reader, on a sixty-second pass against a
thirty-second report. The two clocks drifted against each other, and every time
they crossed, a minute went by whose only sample had already been overwritten:
one row missing, for good, since nothing backfills. Measured on staging before
the write moved, that came to 43 of 1439 minutes in a day — three percent of a
chart whose whole purpose is showing when a machine was in trouble, drawn as
gaps that no outage had caused.

What is left is retention, which is a clock's job and nobody else's:

* **It is nobody's critical path.** Its own task, its own errors. A database
  slow to answer a delete must not delay a heartbeat, tilt the lease reaper or
  make a host look offline, so the call is bounded and a failure ends the pass
  rather than propagating.
* **A pass is bounded.** One batch per pass, against a table that gains a row
  per host per minute. After an outage the backlog drains over several passes
  instead of holding locks for as long as it takes to remove all of it — and
  the work that would delay is recovering jobs.

Missed minutes are still left missed. There is no catch-up pass and no
interpolation: a gap in the curve is a true statement about what was measured,
and the chart is only useful if that statement survives.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cloud_agent.capacity_protocol import CAPACITY_HISTORY_RETENTION_S
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports import HostCapacityRepository

logger = get_logger(__name__)

#: Delay between passes. A minute is far more often than retention needs — the
#: window is a day — and it is what keeps each pass's batch small enough to be
#: bounded, which is the property that matters.
PRUNE_INTERVAL_S = 60.0

#: Rows one pass may remove. Bounded for the same reason the reaper's passes
#: are: after an outage the backlog must drain in batches that do not hold
#: locks long enough to matter.
PRUNE_BATCH_SIZE = 2000

#: A delete that takes longer than this is a database problem, not a slow
#: query, and waiting on it would keep this task alive for no benefit.
PRUNE_TIMEOUT_S = 2.0


async def prune_host_capacity_history(
    store: HostCapacityRepository,
    *,
    interval_seconds: float = PRUNE_INTERVAL_S,
    batch_size: int = PRUNE_BATCH_SIZE,
    timeout_seconds: float = PRUNE_TIMEOUT_S,
) -> None:
    """Delete expired capacity history, one bounded batch per pass, forever.

    Args:
        store: The host-capacity capability.
        interval_seconds: Delay between passes.
        batch_size: Rows one pass may remove.
        timeout_seconds: Ceiling on one database call.
    """
    if interval_seconds <= 0:
        raise ValueError("prune interval must be positive")
    while True:
        await _prune(store, limit=batch_size, timeout_seconds=timeout_seconds)
        await asyncio.sleep(interval_seconds)


async def _prune(store: HostCapacityRepository, *, limit: int, timeout_seconds: float) -> None:
    """Delete one bounded batch of expired history, or give up until next pass.

    Retention failure is logged at warning level and never raised: a full disk
    or a lock conflict must not stop this task, and an unbounded retry loop
    here would be a worse outage than the growth it is trying to prevent.
    """
    try:
        removed = await asyncio.wait_for(
            store.prune_capacity_history(
                older_than_seconds=CAPACITY_HISTORY_RETENTION_S, limit=limit
            ),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        # Shutdown, not failure. The next process resumes from whatever the
        # table already holds; nothing here is worth delaying a stop for.
        raise
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "agent_host_capacity_history_prune_timeout",
            extra={"event": "agent_host_capacity_history_prune_timeout"},
        )
        return
    except Exception:
        logger.warning(
            "agent_host_capacity_history_prune_failed",
            exc_info=True,
            extra={"event": "agent_host_capacity_history_prune_failed"},
        )
        return
    if removed:
        logger.info(
            "agent_host_capacity_history_pruned",
            extra={"event": "agent_host_capacity_history_pruned", "rows": removed},
        )
