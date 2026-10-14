"""Environment maintenance has a deadline and cannot block attempt recovery."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.workers.environment_maintenance import run_environment_maintenance


@pytest.mark.asyncio
async def test_a_stalled_maintenance_pass_times_out_and_retries(caplog) -> None:
    """A stuck database call is cancelled within the worker's pass deadline."""

    async def maintain() -> None:
        if repository.maintain_environments.await_count == 1:
            await asyncio.Event().wait()

    repository = SimpleNamespace(maintain_environments=AsyncMock(side_effect=maintain))

    with caplog.at_level(logging.WARNING, logger="cloud_agent.app"):
        task = asyncio.create_task(
            run_environment_maintenance(repository, interval_seconds=0.02, deadline_seconds=0.01)
        )
        try:
            async with asyncio.timeout(0.5):
                while not repository.maintain_environments.await_count:
                    await asyncio.sleep(0.001)
                while (
                    not any(
                        getattr(record, "event", None) == "agent_environment_maintenance_timeout"
                        for record in caplog.records
                    )
                    or repository.maintain_environments.await_count < 2
                ):
                    await asyncio.sleep(0.001)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert repository.maintain_environments.await_count >= 2
    assert any(
        getattr(record, "event", None) == "agent_environment_maintenance_timeout"
        for record in caplog.records
    )
