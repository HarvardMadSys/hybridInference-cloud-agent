"""Keep environment-run lease recovery off the attempt reaper's critical path."""

from __future__ import annotations

import asyncio
from typing import Protocol

from cloud_agent.logging_utils import get_logger

logger = get_logger("cloud_agent.app")


class _EnvironmentMaintenance(Protocol):
    """Environment maintenance operation owned by the background worker."""

    async def maintain_environments(self) -> None: ...


ENVIRONMENT_MAINTENANCE_INTERVAL_S = 30.0
ENVIRONMENT_MAINTENANCE_DEADLINE_S = ENVIRONMENT_MAINTENANCE_INTERVAL_S


async def run_environment_maintenance(
    repository: _EnvironmentMaintenance,
    *,
    interval_seconds: float = ENVIRONMENT_MAINTENANCE_INTERVAL_S,
    deadline_seconds: float = ENVIRONMENT_MAINTENANCE_DEADLINE_S,
) -> None:
    """Expire stale environment runs and collect archived values periodically.

    A pass has its own deadline and task, so a slow database operation cannot
    delay attempt lease recovery or keep this worker from being cancelled at
    shutdown. A timed-out pass is retried on the next interval.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with asyncio.timeout(deadline_seconds):
                await repository.maintain_environments()
        except TimeoutError:
            logger.warning(
                "Environment maintenance sweep timed out",
                extra={
                    "event": "agent_environment_maintenance_timeout",
                    "deadline_seconds": deadline_seconds,
                },
            )
        except Exception:
            logger.warning(
                "Environment maintenance sweep failed",
                exc_info=True,
                extra={"event": "agent_environment_maintenance_failed"},
            )
