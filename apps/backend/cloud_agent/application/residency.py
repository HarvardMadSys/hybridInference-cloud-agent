"""A residency's lease length, and closing the member processes a release ended.

A residency is a member of a shared sandbox group, renewed by its host's
heartbeat. Three surfaces release residencies (chat deletion, a connection
change, an owner's ended access), and each closes the released processes
here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cloud_agent.ports import HostPoolRepository

#: The moved helper keeps logging under the module it came from: the
#: deployment's log selection is on ``cloud_agent.api.hosts``.
logger = get_logger("cloud_agent.api.hosts")


#: How long a chat deletion waits on hosts after its commit,
#: and how many released residencies it closes at once. A sandbox manager
#: call alone may take a minute to time out; the host's residency heartbeat
#: finishes anything this leaves undone.
RELEASED_RESIDENCY_CLOSE_TIMEOUT_S = 5.0
RELEASED_RESIDENCY_CLOSE_CONCURRENCY = 16


def residency_ttl_s() -> float:
    """Read the current lease duration for newly established or renewed residents."""
    return number_setting("residency_ttl_s")


async def close_released_residencies(
    host_pool: HostPoolRepository,
    released: Iterable[dict[str, Any]],
    *,
    timeout_s: float | None = None,
) -> None:
    """Close each released member process, best-effort.

    The release has already committed, so its generation authorizes nothing
    more. The sandbox manager call closes the process now rather than at the
    host's next residency heartbeat, which retries the same close by itself.

    The owner is waiting on this, so it is bounded: rows proceed concurrently,
    a few at a time, and the whole step stops after ``timeout_s``. A host that
    never answers delays only its own rows. What is cut off is left to that
    heartbeat.
    """
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_host

    rows = list(released)
    if not rows:
        return
    closing = asyncio.Semaphore(RELEASED_RESIDENCY_CLOSE_CONCURRENCY)

    async def close_one(row: dict[str, Any]) -> None:
        async with closing:
            try:
                host = await host_pool.get_host(host=str(row["host"]))
                manager = sandbox_manager_for_host(host) if host is not None else None
                if manager is not None:
                    await manager.shared_residency_lost(
                        str(row["thread_id"]), generation=int(row["generation"])
                    )
            except Exception:
                logger.warning(
                    "agent_released_residency_close_failed",
                    exc_info=True,
                    extra={
                        "event": "agent_released_residency_close_failed",
                        "thread_id": row.get("thread_id"),
                        "generation": row.get("generation"),
                    },
                )

    try:
        async with asyncio.timeout(
            RELEASED_RESIDENCY_CLOSE_TIMEOUT_S if timeout_s is None else timeout_s
        ):
            await asyncio.gather(*(close_one(row) for row in rows))
    except TimeoutError:
        logger.warning(
            "agent_released_residency_close_timed_out",
            extra={
                "event": "agent_released_residency_close_timed_out",
                "threads": [row.get("thread_id") for row in rows],
            },
        )
