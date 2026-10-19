"""Environment and snapshot maintenance cannot block each other or attempt recovery."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.workers.environment_maintenance import run_environment_maintenance


@pytest.mark.parametrize(
    ("blocked_pass", "timeout_event"),
    [
        ("environment", "agent_environment_maintenance_timeout"),
        (
            "snapshot",
            "agent_environment_snapshot_maintenance_timeout",
        ),
    ],
)
async def test_a_stalled_pass_times_out_without_suppressing_the_other(
    caplog, blocked_pass: str, timeout_event: str
) -> None:
    """Each pass gets a separate deadline and the next pass still runs."""
    environment_entered = asyncio.Event()
    snapshot_entered = asyncio.Event()

    async def environment_maintenance() -> None:
        environment_entered.set()
        if blocked_pass == "environment":
            await asyncio.Event().wait()

    async def snapshot_maintenance() -> None:
        snapshot_entered.set()
        if blocked_pass == "snapshot":
            await asyncio.Event().wait()

    environments = SimpleNamespace(
        maintain_environments=AsyncMock(side_effect=environment_maintenance)
    )
    snapshots = SimpleNamespace(maintain=AsyncMock(side_effect=snapshot_maintenance))

    with caplog.at_level(logging.WARNING, logger="cloud_agent.app"):
        task = asyncio.create_task(
            run_environment_maintenance(
                environments,
                snapshots,
                interval_seconds=0.01,
                deadline_seconds=0.01,
            )
        )
        try:
            async with asyncio.timeout(0.5):
                if blocked_pass == "environment":
                    await environment_entered.wait()
                    await snapshot_entered.wait()
                else:
                    await snapshot_entered.wait()
                    while environments.maintain_environments.await_count < 2:
                        await asyncio.sleep(0.001)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    assert environments.maintain_environments.await_count >= 1
    assert snapshots.maintain.await_count >= 1
    assert any(getattr(record, "event", None) == timeout_event for record in caplog.records)


@pytest.mark.parametrize("failed_pass", ["environment", "snapshot"])
async def test_a_failed_pass_does_not_suppress_the_other(failed_pass: str) -> None:
    """A repository error is isolated just like a timeout."""
    environments = SimpleNamespace(
        maintain_environments=AsyncMock(
            side_effect=RuntimeError("environment failed") if failed_pass == "environment" else None
        )
    )
    snapshots = SimpleNamespace(
        maintain=AsyncMock(
            side_effect=RuntimeError("snapshot failed") if failed_pass == "snapshot" else None
        )
    )
    task = asyncio.create_task(
        run_environment_maintenance(
            environments, snapshots, interval_seconds=0.001, deadline_seconds=0.1
        )
    )
    try:
        async with asyncio.timeout(0.5):
            while not snapshots.maintain.await_count or (
                failed_pass == "snapshot" and environments.maintain_environments.await_count < 2
            ):
                await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("blocked_pass", ["environment", "snapshot"])
async def test_cancellation_propagates_from_a_blocked_pass(blocked_pass: str) -> None:
    """Shutdown cancellation stops the current pass without starting an orphan task."""
    entered = asyncio.Event()

    async def blocked() -> None:
        entered.set()
        await asyncio.Event().wait()

    environments = SimpleNamespace(
        maintain_environments=blocked if blocked_pass == "environment" else AsyncMock()
    )
    snapshots = SimpleNamespace(maintain=blocked if blocked_pass == "snapshot" else AsyncMock())
    task = asyncio.create_task(
        run_environment_maintenance(
            environments, snapshots, interval_seconds=0.001, deadline_seconds=10
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
