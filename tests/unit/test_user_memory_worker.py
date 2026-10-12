"""Automatic memory maintenance recovers after failure and stops on shutdown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from cloud_agent.workers.user_memory import refresh_user_memories


async def test_refresh_recovers_after_storage_failure_and_cancellation():
    updated = asyncio.Event()
    calls = 0

    async def refresh(*, limit):
        nonlocal calls
        assert limit == 100
        calls += 1
        if calls == 1:
            raise RuntimeError("Temporary failure")
        updated.set()
        return 1

    repository = AsyncMock()
    repository.refresh_active_users.side_effect = refresh
    task = asyncio.create_task(refresh_user_memories(repository, interval_seconds=0.001))
    try:
        await asyncio.wait_for(updated.wait(), timeout=1)
        assert calls >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
