"""Expired archives are purged periodically, and one failed pass never stops the loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from cloud_agent.workers import agent_memory as worker


async def test_maintenance_purges_and_survives_failures():
    purged = asyncio.Event()
    calls = 0

    async def purge():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        purged.set()

    repository = AsyncMock()
    repository.purge_memories.side_effect = purge
    task = asyncio.create_task(worker.maintain_agent_memories(repository, interval_seconds=0.001))
    try:
        await asyncio.wait_for(purged.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 2
