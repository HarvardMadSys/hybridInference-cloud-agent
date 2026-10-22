"""Keep environment-run lease recovery off the attempt reaper's critical path."""

from __future__ import annotations

from typing import Protocol

from cloud_agent.workers.periodic import run_every, run_pass


class _EnvironmentMaintenance(Protocol):
    """Environment maintenance operation owned by the background worker."""

    async def maintain_environments(self) -> None: ...


class _SnapshotMaintenance(Protocol):
    """Snapshot retirement operation owned by the background worker."""

    async def maintain(self) -> None: ...


ENVIRONMENT_MAINTENANCE_INTERVAL_S = 30.0
ENVIRONMENT_MAINTENANCE_DEADLINE_S = ENVIRONMENT_MAINTENANCE_INTERVAL_S


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

    async def expire_runs() -> None:
        await run_pass(
            environments.maintain_environments,
            deadline_seconds=deadline_seconds,
            description="Environment maintenance sweep",
            event="agent_environment_maintenance",
        )

    async def retire_snapshots() -> None:
        await run_pass(
            snapshots.maintain,
            deadline_seconds=deadline_seconds,
            description="Environment snapshot maintenance sweep",
            event="agent_environment_snapshot_maintenance",
        )

    await run_every(interval_seconds, expire_runs, retire_snapshots)
