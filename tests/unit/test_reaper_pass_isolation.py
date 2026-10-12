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

from cloud_agent.workers.attempts import reap_expired_attempts

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
