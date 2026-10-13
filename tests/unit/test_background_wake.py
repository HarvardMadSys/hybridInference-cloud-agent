"""The control plane's own workers wake on a notification, not only their clocks.

What has to hold: a snapshot that becomes due is captured, a captured snapshot
is published, and a scheduled usage read is made when it is due, without
waiting out the clock each worker used to sleep on. An idle worker sleeps until
the earliest retry it scheduled; a pass handles everything due, past a failure;
and while nothing listens each worker looks on its old clock. The trigger and
schedule side is in the ``dbtest`` tier.
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


async def _listening() -> BackgroundWake:
    """A wake-up whose connection is listening, its connect wake-ups consumed."""
    wake = BackgroundWake()
    wake.reset(listening=True)
    for kind in (SNAPSHOT, PUBLISH, LEDGER):
        assert await wake.wait(kind, 0)
    return wake


async def test_each_worker_is_woken_by_its_own_name_and_a_reset_wakes_all():
    wake = BackgroundWake()
    assert wake.listening is False
    wake.notified(PUBLISH)
    wake.notified("nobody")
    assert await wake.wait(PUBLISH, 0) is True
    assert await wake.wait(PUBLISH, 0.01) is False, "a wake-up is consumed"
    assert await wake.wait(SNAPSHOT, 0.01) is False

    wake.reset(listening=True)
    assert wake.listening is True
    wake.reset(listening=False)
    assert wake.listening is False
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


async def test_one_pass_captures_every_due_workspace(monkeypatch):
    outcomes = [True, True, True, False]
    captured = AsyncMock(side_effect=lambda store: outcomes.pop(0))
    monkeypatch.setattr(snapshots, "_capture_due_shared_snapshot", captured)
    store = SimpleNamespace(
        snapshot=SimpleNamespace(next_snapshot_retry_due=AsyncMock(return_value=None))
    )
    wake = await _listening()
    task = asyncio.create_task(
        snapshots.coordinate_shared_snapshots(store, interval_seconds=30, wake=wake)  # type: ignore[arg-type]
    )
    try:
        await asyncio.sleep(0.05)
        wake.notified(SNAPSHOT)
        await _eventually(lambda: captured.await_count == 4)
        await asyncio.sleep(0.05)
        assert captured.await_count == 4, "nothing more until the next wake-up"
    finally:
        await _stop(task)


async def test_an_idle_coordinator_looks_again_when_a_deferred_capture_is_due(monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_IDLE_S", 30.0)
    captured = AsyncMock(return_value=False)
    monkeypatch.setattr(snapshots, "_capture_due_shared_snapshot", captured)
    due = datetime.now(UTC) + timedelta(seconds=0.1)
    store = SimpleNamespace(
        snapshot=SimpleNamespace(next_snapshot_retry_due=AsyncMock(return_value=due))
    )
    task = asyncio.create_task(
        snapshots.coordinate_shared_snapshots(store, interval_seconds=30, wake=await _listening())  # type: ignore[arg-type]
    )
    try:
        await _eventually(lambda: captured.await_count >= 1)
    finally:
        await _stop(task)


async def test_while_nothing_listens_each_worker_looks_on_its_old_clock(monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_IDLE_S", 30.0)
    monkeypatch.setattr(publish_worker, "PUBLISH_IDLE_S", 30.0)
    monkeypatch.setattr(ledger, "LEDGER_IDLE_S", 30.0)
    monkeypatch.setattr(ledger, "REFRESH_POLL_SECONDS", 0.01)
    captured = AsyncMock(return_value=False)
    monkeypatch.setattr(snapshots, "_capture_due_shared_snapshot", captured)
    imported = AsyncMock()
    monkeypatch.setattr(publish_worker, "_retry_due_base_imports", imported)
    monkeypatch.setattr(publish_worker, "_publish_due", AsyncMock())
    collector_store = SimpleNamespace(
        claim_due_ledger_refresh=AsyncMock(return_value=None),
        next_ledger_refresh_due=AsyncMock(return_value=None),
    )
    wake = BackgroundWake()  # never connected
    tasks = [
        asyncio.create_task(
            snapshots.coordinate_shared_snapshots(
                SimpleNamespace(), interval_seconds=0.01, wake=wake
            )  # type: ignore[arg-type]
        ),
        asyncio.create_task(
            publish_worker.publish_loop(
                SimpleNamespace(),  # type: ignore[arg-type]
                credential_provider=lambda: publish_worker.GitHubCredential("token"),
                interval_seconds=0.01,
                wake=wake,
            )
        ),
        asyncio.create_task(
            ledger.collect_usage_snapshots(collector_store, SimpleNamespace(), wake=wake)  # type: ignore[arg-type]
        ),
    ]
    try:
        await _eventually(
            lambda: (
                captured.await_count >= 3
                and imported.await_count >= 3
                and collector_store.claim_due_ledger_refresh.await_count >= 3
            )
        )
    finally:
        for task in tasks:
            await _stop(task)


async def test_a_captured_snapshot_is_published_when_announced(monkeypatch):
    imported = AsyncMock()
    published = AsyncMock(return_value=None)
    monkeypatch.setattr(publish_worker, "_retry_due_base_imports", imported)
    monkeypatch.setattr(publish_worker, "_publish_due", published)
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


async def test_one_pass_publishes_past_a_failure_and_retries_every_due_import(monkeypatch):
    claims = [{"job_id": "fails"}, {"job_id": "opens"}]
    published: list[str] = []

    async def publish_claimed(store, job, **kwargs):
        published.append(job["job_id"])
        return None if job["job_id"] == "fails" else "https://example.test/pr/1"

    monkeypatch.setattr(publish_worker, "_publish_claimed", publish_claimed)
    # An import whose failure was not scheduled comes back at once: it is
    # tried once in the pass, not in a loop.
    stuck = {"snapshot_id": 3}
    retried = AsyncMock(return_value=False)
    monkeypatch.setattr(publish_worker, "_retry_base_import", retried)
    store = SimpleNamespace(
        snapshot=SimpleNamespace(
            claim_for_publish=AsyncMock(side_effect=lambda: claims.pop(0) if claims else None),
            pending_snapshot_base_import=AsyncMock(return_value=stuck),
            next_base_import_retry_due=AsyncMock(return_value=None),
        )
    )
    wake = await _listening()
    task = asyncio.create_task(
        publish_worker.publish_loop(
            store,  # type: ignore[arg-type]
            credential_provider=lambda: publish_worker.GitHubCredential("token"),
            interval_seconds=30,
            wake=wake,
        )
    )
    try:
        wake.notified(PUBLISH)
        await _eventually(lambda: published == ["fails", "opens"])
        assert retried.await_count == 1
    finally:
        await _stop(task)


@pytest.mark.parametrize("retry_kind", ["base_import", "snapshot_publish"])
async def test_an_idle_publisher_looks_again_when_a_retry_is_due(monkeypatch, retry_kind):
    monkeypatch.setattr(publish_worker, "PUBLISH_IDLE_S", 30.0)
    imported = AsyncMock()
    monkeypatch.setattr(publish_worker, "_retry_due_base_imports", imported)
    monkeypatch.setattr(publish_worker, "_publish_due", AsyncMock())
    due = datetime.now(UTC) + timedelta(seconds=0.1)
    schedules = {
        "next_base_import_retry_due": AsyncMock(return_value=None),
        "next_snapshot_publish_retry_due": AsyncMock(return_value=None),
    }
    schedules[f"next_{retry_kind}_retry_due"].return_value = due
    store = SimpleNamespace(snapshot=SimpleNamespace(**schedules))
    task = asyncio.create_task(
        publish_worker.publish_loop(
            store,  # type: ignore[arg-type]
            credential_provider=lambda: publish_worker.GitHubCredential("token"),
            interval_seconds=30,
            wake=await _listening(),
        )
    )
    try:
        await _eventually(lambda: imported.await_count >= 1)
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
        ledger.collect_usage_snapshots(store, SimpleNamespace(), wake=await _listening())  # type: ignore[arg-type]
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
    wake = await _listening()
    task = asyncio.create_task(ledger.collect_usage_snapshots(store, SimpleNamespace(), wake=wake))  # type: ignore[arg-type]
    try:
        await _eventually(lambda: store.claim_due_ledger_refresh.await_count == 1)
        await asyncio.sleep(0.05)
        assert store.claim_due_ledger_refresh.await_count == 1, "nothing due, nothing read"
        wake.notified(LEDGER)
        await _eventually(lambda: store.claim_due_ledger_refresh.await_count == 2)
    finally:
        await _stop(task)
