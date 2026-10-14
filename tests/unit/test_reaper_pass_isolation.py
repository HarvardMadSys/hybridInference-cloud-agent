"""One recovery pass of the reaper failing does not skip the others.

The attempt reaper, the stalled-capture reaper, the stalled-publish reaper and
the stalled-Fork reaper recover different things. They shared one ``try``, so a
pass that failed every time -- a capture the database refused did, for every
workspace in the deployment -- also stopped stalled publishes and Forks from
ever being recovered.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.support.storage import OneFake

from cloud_agent.workers import attempts
from cloud_agent.workers.attempts import reap_expired_attempts
from cloud_agent.workers.environment_maintenance import run_environment_maintenance

pytestmark = pytest.mark.asyncio


async def test_each_stalled_work_reaper_runs_when_the_ones_before_it_fail() -> None:
    """Every reaper is asked once per pass, whichever of the others raised."""
    inner = SimpleNamespace(
        reap_expired=AsyncMock(side_effect=RuntimeError("attempt pass failed")),
        reap_stalled_sandbox_snapshot_captures=AsyncMock(
            side_effect=RuntimeError("capture pass failed")
        ),
        reap_stalled_snapshot_publishes=AsyncMock(side_effect=RuntimeError("publish pass failed")),
        reap_stalled_session_forks=AsyncMock(return_value=[]),
    )
    task = asyncio.create_task(
        reap_expired_attempts(OneFake(inner), interval_seconds=0.001, grants=None)
    )
    try:
        async with asyncio.timeout(10):
            while not inner.reap_stalled_session_forks.await_count:
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert inner.reap_expired.await_count
    assert inner.reap_stalled_sandbox_snapshot_captures.await_count
    assert inner.reap_stalled_snapshot_publishes.await_count


async def test_stalled_environment_maintenance_does_not_delay_attempt_reaping() -> None:
    """Environment database work is owned by a worker outside the reaper."""
    maintenance_started = asyncio.Event()

    async def stalled_maintenance() -> None:
        maintenance_started.set()
        await asyncio.Event().wait()

    inner = SimpleNamespace(
        environments=SimpleNamespace(maintain_environments=stalled_maintenance),
        reap_expired=AsyncMock(return_value=[]),
        reap_stalled_sandbox_snapshot_captures=AsyncMock(return_value=[]),
        reap_stalled_snapshot_publishes=AsyncMock(return_value=[]),
        reap_stalled_session_forks=AsyncMock(return_value=[]),
    )
    store = OneFake(inner)
    maintenance = asyncio.create_task(
        run_environment_maintenance(inner.environments, interval_seconds=0.001, deadline_seconds=1),
        name="stalled-environment-maintenance",
    )
    reaper = asyncio.create_task(reap_expired_attempts(store, interval_seconds=0.001, grants=None))
    try:
        async with asyncio.timeout(0.5):
            await maintenance_started.wait()
            while not inner.reap_expired.await_count:
                await asyncio.sleep(0.001)
    finally:
        reaper.cancel()
        maintenance.cancel()
        await asyncio.gather(reaper, maintenance, return_exceptions=True)

    assert inner.reap_expired.await_count


@pytest.mark.parametrize("blocked_pass", ["follow_ups", "goal_wakeups", "goal_scheduling"])
async def test_stalled_synchronous_pre_reaper_pass_delays_attempt_reaping(
    monkeypatch: pytest.MonkeyPatch, blocked_pass: str
) -> None:
    """Fault injection records the remaining synchronous work ahead of lease recovery."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stall(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    inner = SimpleNamespace(
        reap_offline_hosts=AsyncMock(return_value=[]),
        reap_expired_residencies=AsyncMock(return_value=[]),
        sweep_queued_follow_ups=AsyncMock(
            side_effect=stall if blocked_pass == "follow_ups" else None
        ),
        sweep_goal_scheduling=AsyncMock(
            side_effect=(
                stall
                if blocked_pass == "goal_scheduling"
                else lambda: {"rearmed": [], "stalled": []}
            )
        ),
        reap_expired=AsyncMock(return_value=[]),
        reap_stalled_sandbox_snapshot_captures=AsyncMock(return_value=[]),
        reap_stalled_snapshot_publishes=AsyncMock(return_value=[]),
        reap_stalled_session_forks=AsyncMock(return_value=[]),
        purge_stale_staged=AsyncMock(return_value=0),
        threads_to_settle=AsyncMock(return_value=[]),
    )
    goal_wakeups = AsyncMock(side_effect=stall if blocked_pass == "goal_wakeups" else None)
    monkeypatch.setattr("cloud_agent.workers.attempts.run_goal_wakeups", goal_wakeups)
    task = asyncio.create_task(
        attempts._run_attempt_reaper(OneFake(inner), interval_seconds=0.001, grants=None)
    )
    try:
        async with asyncio.timeout(1):
            await entered.wait()
        assert not inner.reap_expired.await_count
        release.set()
        async with asyncio.timeout(1):
            while not inner.reap_expired.await_count:
                await asyncio.sleep(0.001)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert inner.reap_expired.await_count
