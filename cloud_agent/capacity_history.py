"""Persisting the host-capacity history the Servers chart reads.

The chart has to keep filling while nobody is looking. A browser that has to
stay open to accumulate a curve is not monitoring — it is a session, and the
question an operator actually asks ("has this machine been pinned all
afternoon, or did it just spike?") cannot be answered by a page that was only
opened a minute ago.

So the control plane writes one observation per host per minute, from the
snapshots the heartbeats already deposited, into a bounded table. Three
properties matter more than throughput here:

* **It is nobody's critical path.** A separate task, its own loop, its own
  errors. A database that is slow to answer the history batch must not delay a
  heartbeat, tilt the lease reaper, or make a host look offline — so every
  await in a round is bounded and every failure ends the round rather than
  propagating.
* **A round always finishes.** Per-batch and per-round budgets, and a keyset
  cursor that survives into the next round. The alternative — restarting from
  the first host every time — starves everything behind whichever host is slow,
  and does it silently, because the round still "succeeded".
* **Repeating anything is harmless.** Selection is by host, so a crash between
  two batches is resumed rather than reconstructed; the write is
  ``ON CONFLICT DO NOTHING`` against the minute and against the sample id, so
  two control planes or two overlapping rounds cannot double-write a point or
  produce a second one from the same observation.

Missed minutes are left missed. There is no catch-up pass and no interpolation:
a gap in the curve is a true statement about what was measured, and the chart
is only useful if that statement survives.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from cloud_agent.capacity_protocol import history_point_write_from_row
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage.store import CAPACITY_HISTORY_RETENTION_S

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)

#: One observation per minute, so one pass per minute. Running more often would
#: cost a read per host and change nothing: the conflict clause would discard
#: every point but the first.
HISTORY_INTERVAL_S = 60.0

#: Hosts per page. The performance target is a hundred-host pool, so a round is
#: normally one page; the ceiling is what keeps that true if it grows.
HISTORY_BATCH_SIZE = 100

#: A batch that takes longer than this is a database problem, not a slow query,
#: and waiting for it would spend the round's whole budget on one page.
HISTORY_BATCH_TIMEOUT_S = 2.0

#: Total time one round may spend before yielding to the next. The interval is
#: longer than this on purpose, so a struggling database produces gaps in the
#: curve rather than overlapping rounds.
HISTORY_ROUND_BUDGET_S = 10.0

#: Rows one cleanup pass may remove. Bounded for the same reason the reaper's
#: passes are: after an outage the backlog must drain in batches that do not
#: hold locks long enough to matter.
HISTORY_PRUNE_BATCH_SIZE = 2000


async def collect_host_capacity_history(
    store: AgentJobStore,
    *,
    interval_seconds: float = HISTORY_INTERVAL_S,
    batch_size: int = HISTORY_BATCH_SIZE,
    batch_timeout_seconds: float = HISTORY_BATCH_TIMEOUT_S,
    round_budget_seconds: float = HISTORY_ROUND_BUDGET_S,
    prune_batch_size: int = HISTORY_PRUNE_BATCH_SIZE,
    clock: Callable[[], float] | None = None,
) -> None:
    """Record one capacity observation per host per minute, and prune old ones.

    Args:
        store: The agent store.
        interval_seconds: Delay between rounds.
        batch_size: Hosts per page.
        batch_timeout_seconds: Ceiling on one database batch.
        round_budget_seconds: Ceiling on one whole round.
        prune_batch_size: Rows one cleanup pass may remove.
        clock: Monotonic seconds for the round budget. Defaults to the running
            loop's own clock; a test supplies its own so the budget rule can be
            checked without waiting for real time to pass.
    """
    if interval_seconds <= 0:
        raise ValueError("history interval must be positive")
    tick = clock if clock is not None else (lambda: asyncio.get_running_loop().time())
    cursor: str | None = None
    while True:
        try:
            cursor = await _record_round(
                store,
                cursor,
                batch_size=batch_size,
                batch_timeout_seconds=batch_timeout_seconds,
                round_budget_seconds=round_budget_seconds,
                prune_batch_size=prune_batch_size,
                clock=tick,
            )
        except asyncio.CancelledError:
            # Shutdown, not failure. The next process resumes from whatever the
            # table already holds; nothing here is worth delaying a stop for.
            raise
        except Exception:
            logger.warning(
                "agent_host_capacity_history_round_failed",
                exc_info=True,
                extra={"event": "agent_host_capacity_history_round_failed"},
            )
            cursor = None
        await asyncio.sleep(interval_seconds)


async def _record_round(
    store: AgentJobStore,
    cursor: str | None,
    *,
    batch_size: int,
    batch_timeout_seconds: float,
    round_budget_seconds: float,
    prune_batch_size: int,
    clock: Callable[[], float],
) -> str | None:
    """Run one round, returning the cursor the next round should resume from.

    The budget is checked before every write, not only before every page. A
    page is up to a hundred hosts and each write has its own timeout, so a
    database that has become slow turns one page into a hundred timeouts —
    a round that runs a hundred times longer than it was allowed to while
    still reporting that it finished. The cursor advances per host for the
    same reason: each round visits every host in turn, and a host whose write
    failed — a timeout, a corrupt snapshot — leaves a gap in its own curve
    rather than stalling every host behind it. Missed minutes are not
    reconstructed; that is the whole reason the cursor may move past one.

    Returns:
        The last host written, when the round stopped early with work left; or
        ``None`` when the pool was walked to its end, so the next round starts
        from the beginning and picks up hosts that appeared meanwhile.
    """
    deadline = clock() + round_budget_seconds
    await _prune(
        store,
        limit=prune_batch_size,
        timeout_seconds=_remaining(deadline, clock, batch_timeout_seconds),
    )
    while True:
        remaining = _remaining(deadline, clock, batch_timeout_seconds)
        if remaining <= 0:
            _log_truncated()
            return cursor
        try:
            rows = await asyncio.wait_for(
                store.list_capacity_history_candidates(after_host=cursor, limit=batch_size),
                timeout=remaining,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "agent_host_capacity_history_read_timeout",
                extra={"event": "agent_host_capacity_history_read_timeout"},
            )
            return cursor
        except Exception:
            # The cursor is kept, not reset. A page that failed is a page this
            # round did not cover, and starting over from the first host would
            # leave everything behind the failure unmeasured for as long as the
            # failure lasts — which is exactly when the curve matters most.
            logger.warning(
                "agent_host_capacity_history_read_failed",
                exc_info=True,
                extra={"event": "agent_host_capacity_history_read_failed"},
            )
            return cursor
        if not rows:
            return None
        for row in rows:
            remaining = _remaining(deadline, clock, batch_timeout_seconds)
            if remaining <= 0:
                _log_truncated()
                return cursor
            await _write_point(store, row, timeout_seconds=remaining)
            cursor = str(row["host"])
        if len(rows) < batch_size:
            return None


def _remaining(deadline: float, clock: Callable[[], float], ceiling: float) -> float:
    """How long this operation may take without overrunning the round.

    A budget that only gates *whether* an operation starts is a soft one: the
    last write still gets its full timeout, so a round configured for ten
    seconds can run to twelve. Clamping each timeout to what is left makes the
    budget the thing that actually bounds the round.

    Args:
        deadline: Monotonic instant the round must be done by.
        clock: Monotonic seconds.
        ceiling: The operation's own timeout, used when there is room for it.

    Returns:
        The smaller of the two, never negative. Zero means the round is over.
    """
    return max(0.0, min(ceiling, deadline - clock()))


def _log_truncated() -> None:
    """Record that a round yielded to the next one rather than finishing."""
    logger.info(
        "agent_host_capacity_history_round_truncated",
        extra={"event": "agent_host_capacity_history_round_truncated"},
    )


async def _write_point(
    store: AgentJobStore, row: dict[str, Any], *, timeout_seconds: float
) -> None:
    """Persist one candidate row, isolating its failures from the round.

    A row that cannot be converted — a hand-edited snapshot, a value outside
    the column's own constraints — costs this host this minute and nothing
    else. The next heartbeat replaces the snapshot and the next minute is
    written normally.
    """
    point = history_point_write_from_row(row)
    if point is None:
        logger.warning(
            "agent_host_capacity_history_point_skipped",
            extra={
                "event": "agent_host_capacity_history_point_skipped",
                "host": str(row.get("host")),
            },
        )
        return
    try:
        await asyncio.wait_for(
            store.record_capacity_history_point(
                host=str(row["host"]),
                capacity_epoch=row["capacity_epoch"],
                point=point,
            ),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "agent_host_capacity_history_write_timeout",
            extra={
                "event": "agent_host_capacity_history_write_timeout",
                "host": str(row.get("host")),
            },
        )
    except Exception:
        logger.warning(
            "agent_host_capacity_history_write_failed",
            exc_info=True,
            extra={
                "event": "agent_host_capacity_history_write_failed",
                "host": str(row.get("host")),
            },
        )


async def _prune(store: AgentJobStore, *, limit: int, timeout_seconds: float) -> None:
    """Delete one bounded batch of expired history, or give up until next round.

    Retention failure is logged at warning level and never raised: a full disk
    or a lock conflict must not stop the sampler, and an unbounded retry loop
    here would be a worse outage than the growth it is trying to prevent.
    """
    try:
        removed = await asyncio.wait_for(
            store.prune_capacity_history(
                older_than_seconds=CAPACITY_HISTORY_RETENTION_S, limit=limit
            ),
            timeout=timeout_seconds,
        )
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
