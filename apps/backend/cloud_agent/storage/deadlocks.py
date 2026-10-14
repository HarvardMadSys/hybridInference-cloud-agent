"""Running a write again when Postgres aborted it to break a lock cycle.

The lock order in :mod:`cloud_agent.storage` is what keeps writers from
deadlocking, and a cycle is fixed by fixing the order. This is for the few
writes whose loss costs more than doing them twice, should a cycle turn up that
the order missed: Postgres aborts one transaction of the cycle and lets the
other through, so the victim's next try normally finds nothing to wait for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import asyncpg

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

_T = TypeVar("_T")

#: How many times such a write is tried in all, the first try included.
DEADLOCK_TRIES = 3


async def retry_deadlock_victim(write: str, transaction: Callable[[], Awaitable[_T]]) -> _T:
    """Run ``transaction``, and run it again while Postgres picks it as a deadlock's victim.

    ``transaction`` opens and commits a transaction of its own, so an aborted
    try leaves nothing behind and the next starts clean. Each retry is logged
    under ``write``, so a cycle the lock order missed stays visible as one; the
    last try's error is raised.
    """
    for _ in range(DEADLOCK_TRIES - 1):
        try:
            return await transaction()
        except asyncpg.exceptions.DeadlockDetectedError:
            logger.warning(
                "agent_storage_deadlock_retried",
                exc_info=True,
                extra={"event": "agent_storage_deadlock_retried", "write": write},
            )
    return await transaction()
