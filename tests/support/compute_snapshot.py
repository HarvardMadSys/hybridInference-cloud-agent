"""Adapt existing in-memory compute fixtures to the bulk snapshot port."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_agent.ports.compute_resources import ComputeSchedulingSnapshot


def snapshot_repository(**kwargs):
    repository = SimpleNamespace(**kwargs)

    async def snapshot():
        allocations = await repository.list_allocations()
        operations, lifecycle = {}, {}
        for row in allocations:
            operations[row["id"]] = await repository.list_operations(allocation_id=row["id"])
            lifecycle[row["id"]] = await repository.list_operations(
                allocation_id=row["id"], kinds=["extend", "rotate"]
            )
        return ComputeSchedulingSnapshot(allocations, operations, lifecycle)

    repository.scheduling_inventory = AsyncMock(side_effect=snapshot)
    return repository
