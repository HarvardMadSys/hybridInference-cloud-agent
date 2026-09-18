"""Scheduling and failure boundaries for shared publication capture."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cloud_agent.workers import snapshots as control_plane


class _SnapshotPort:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once
        self.called = asyncio.Event()

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("temporary database failure")
        self.called.set()
        return None


class _ClaimStore:
    """Shaped like the assembled ``Storage``: capabilities hang off named ports.

    The claim used to be reached for with ``getattr(store, ...)`` against the
    top-level store, and a fake carrying the method there kept these tests
    green while the real composition root -- which has it on ``.snapshot`` --
    silently claimed nothing. Keeping the fake's shape honest is what makes a
    coordinator that reaches for the wrong object fail here.
    """

    def __init__(self, *, fail_once: bool = False) -> None:
        self.snapshot = _SnapshotPort(fail_once=fail_once)

    @property
    def calls(self) -> int:
        return self.snapshot.calls

    @property
    def called(self) -> asyncio.Event:
        return self.snapshot.called


@pytest.mark.asyncio
async def test_snapshot_coordinator_runs_on_its_own_short_schedule():
    store = _ClaimStore()
    task = asyncio.create_task(
        control_plane.coordinate_shared_snapshots(store, interval_seconds=0.001)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(store.called.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.calls >= 1


@pytest.mark.asyncio
async def test_snapshot_coordinator_survives_one_failed_pass():
    store = _ClaimStore(fail_once=True)
    task = asyncio.create_task(
        control_plane.coordinate_shared_snapshots(store, interval_seconds=0.001)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(store.called.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.calls >= 2


def test_snapshot_coordinator_rejects_a_non_positive_interval():
    async def run() -> None:
        await control_plane.coordinate_shared_snapshots(  # type: ignore[arg-type]
            _ClaimStore(), interval_seconds=0
        )

    with pytest.raises(ValueError, match="positive"):
        asyncio.run(run())
