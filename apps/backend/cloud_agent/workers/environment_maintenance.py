"""Keep environment-run lease recovery off the attempt reaper's critical path."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from cloud_agent.logging_utils import get_logger

logger = get_logger("cloud_agent.app")


class _EnvironmentMaintenance(Protocol):
    """Environment maintenance operation owned by the background worker."""

    async def maintain_environments(self) -> None: ...


class _SnapshotMaintenance(Protocol):
    """Snapshot retirement operation owned by the background worker."""

    async def maintain(self) -> None: ...


ENVIRONMENT_MAINTENANCE_INTERVAL_S = 30.0
ENVIRONMENT_MAINTENANCE_DEADLINE_S = ENVIRONMENT_MAINTENANCE_INTERVAL_S


async def _run_pass(
    name: str,
    operation: Callable[[], Awaitable[None]],
    *,
    deadline_seconds: float,
) -> None:
    display_name = name.replace("_", " ").capitalize()
    try:
        async with asyncio.timeout(deadline_seconds):
            await operation()
    except TimeoutError:
        logger.warning(
            "%s maintenance sweep timed out",
            display_name,
            extra={
                "event": f"agent_{name}_maintenance_timeout",
                "deadline_seconds": deadline_seconds,
            },
        )
    except Exception:
        logger.warning(
            "%s maintenance sweep failed",
            display_name,
            exc_info=True,
            extra={"event": f"agent_{name}_maintenance_failed"},
        )


async def run_environment_maintenance(
    environments: _EnvironmentMaintenance,
    snapshots: _SnapshotMaintenance,
    *,
    interval_seconds: float = ENVIRONMENT_MAINTENANCE_INTERVAL_S,
    deadline_seconds: float = ENVIRONMENT_MAINTENANCE_DEADLINE_S,
) -> None:
    """Expire stale environment runs and retire unused snapshots periodically.

    Each database pass has its own deadline and exception boundary. A stalled
    environment-run query cannot suppress snapshot retirement, and a stalled
    snapshot query cannot suppress environment-run cleanup. Neither can delay
    attempt lease recovery, and both remain cancellable at shutdown.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await _run_pass(
            "environment",
            environments.maintain_environments,
            deadline_seconds=deadline_seconds,
        )
        await _run_pass(
            "environment_snapshot",
            snapshots.maintain,
            deadline_seconds=deadline_seconds,
        )
