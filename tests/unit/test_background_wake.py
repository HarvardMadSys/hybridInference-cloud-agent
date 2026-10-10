"""The control plane's own workers wake on a notification, not only their clocks.

What has to hold: a snapshot that becomes due is captured, a captured snapshot
is published, and a scheduled usage read is made when it is due, without
waiting out the clock each worker used to sleep on. The clock stays as the
backstop: each worker still runs on it when nothing wakes it. The trigger side
is in the ``dbtest`` tier.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent import ledger, publish_worker
from cloud_agent.workers import snapshots
from cloud_agent.workers.background_wake import LEDGER, PUBLISH, SNAPSHOT, BackgroundWake

#: Well under every worker's clock, so a worker that only slept would miss it.
AT_ONCE_S = 0.5


async def _eventually(predicate, timeout_s: float = AT_ONCE_S) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("the worker did not run in time")
        await asyncio.sleep(0.005)


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_each_worker_is_woken_by_its_own_name_and_a_reset_wakes_all():
    wake = BackgroundWake()
    wake.notified(PUBLISH)
    wake.notified("nobody")
    assert await wake.wait(PUBLISH, 0) is True
    assert await wake.wait(PUBLISH, 0.01) is False, "a wake-up is consumed"
    assert await wake.wait(SNAPSHOT, 0.01) is False

    wake.reset(listening=False)
    assert all([await wake.wait(kind, 0) for kind in (SNAPSHOT, PUBLISH, LEDGER)])


async def test_a_due_snapshot_is_captured_when_announced_not_at_the_next_tick():
    claimed = AsyncMock(return_value=None)
    store = SimpleNamespace(snapshot=SimpleNamespace(claim_due_sandbox_snapshot=claimed))
    wake = BackgroundWake()
    task = asyncio.create_task(
        snapshots.coordinate_shared_snapshots(store, interval_seconds=30, wake=wake)  # type: ignore[arg-type]
    )
    try:
        await asyncio.sleep(0.05)
        assert claimed.await_count == 0
        wake.notified(SNAPSHOT)
        await _eventually(lambda: claimed.await_count == 1)
    finally:
        await _stop(task)


async def test_a_captured_snapshot_is_published_when_announced(monkeypatch):
    imported = AsyncMock()
    published = AsyncMock(return_value=None)
    monkeypatch.setattr(publish_worker, "retry_snapshot_base_import", imported)
    monkeypatch.setattr(publish_worker, "publish_one", published)
    wake = BackgroundWake()
    task = asyncio.create_task(
        publish_worker.publish_loop(
            SimpleNamespace(),  # type: ignore[arg-type]
            credential_provider=lambda: publish_worker.GitHubCredential("token"),
            interval_seconds=30,
            wake=wake,
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert published.await_count == 0
        wake.notified(PUBLISH)
        await _eventually(lambda: published.await_count == 1)
        assert imported.await_count == 1
    finally:
        await _stop(task)


async def test_an_idle_collector_reads_when_the_earliest_schedule_is_due(monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_IDLE_S", 30.0)
    due = datetime.now(UTC) + timedelta(seconds=0.1)
    store = SimpleNamespace(
        claim_due_ledger_refresh=AsyncMock(return_value=None),
        next_ledger_refresh_due=AsyncMock(return_value=due),
    )
    task = asyncio.create_task(
        ledger.collect_usage_snapshots(store, SimpleNamespace(), wake=BackgroundWake())  # type: ignore[arg-type]
    )
    try:
        await _eventually(lambda: store.claim_due_ledger_refresh.await_count >= 2)
    finally:
        await _stop(task)


async def test_an_idle_collector_with_nothing_scheduled_waits_for_its_wake_up(monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_IDLE_S", 30.0)
    store = SimpleNamespace(
        claim_due_ledger_refresh=AsyncMock(return_value=None),
        next_ledger_refresh_due=AsyncMock(return_value=None),
    )
    wake = BackgroundWake()
    task = asyncio.create_task(ledger.collect_usage_snapshots(store, SimpleNamespace(), wake=wake))  # type: ignore[arg-type]
    try:
        await _eventually(lambda: store.claim_due_ledger_refresh.await_count == 1)
        await asyncio.sleep(0.05)
        assert store.claim_due_ledger_refresh.await_count == 1, "nothing due, nothing read"
        wake.notified(LEDGER)
        await _eventually(lambda: store.claim_due_ledger_refresh.await_count == 2)
    finally:
        await _stop(task)
