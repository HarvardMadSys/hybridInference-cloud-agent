"""Queue and goal recovery cannot hold up attempt lease recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cloud_agent.workers import attempts, recovery_maintenance


def _reaper_store(attempt_reaped: asyncio.Event) -> SimpleNamespace:
    async def reap_expired(*, max_attempts: int) -> list[object]:
        assert max_attempts == attempts.MAX_ATTEMPTS
        attempt_reaped.set()
        return []

    return SimpleNamespace(
        host_pool=object(),
        residency=object(),
        attachments=SimpleNamespace(purge_stale_staged=_noop),
        attempt_commands=SimpleNamespace(reap_expired=reap_expired),
        snapshot=SimpleNamespace(
            reap_stalled_sandbox_snapshot_captures=_empty,
            reap_stalled_snapshot_publishes=_empty,
        ),
        session_fork=SimpleNamespace(reap_stalled_session_forks=_empty),
    )


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


async def _empty() -> list[object]:
    return []


@pytest.mark.parametrize(
    "blocked_operation", ["queued_follow_ups", "goal_wakeups", "goal_scheduling"]
)
async def test_blocked_recovery_does_not_delay_attempt_reaping(
    monkeypatch: pytest.MonkeyPatch, blocked_operation: str
) -> None:
    operation_started = asyncio.Event()
    operation_cancelled = asyncio.Event()
    attempt_reaped = asyncio.Event()
    monkeypatch.setattr(attempts, "_reap_offline_hosts", _noop)
    monkeypatch.setattr(attempts, "_reap_lapsed_residencies", _noop)
    monkeypatch.setattr(attempts, "_cleanup_reaped_shared_residencies", _noop)
    monkeypatch.setattr(attempts, "settle_waiting_queues", _noop)

    async def block(*_args: object, **_kwargs: object) -> None:
        operation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_cancelled.set()

    store = _reaper_store(attempt_reaped)
    if blocked_operation == "queued_follow_ups":
        store.attempt_commands.sweep_queued_follow_ups = block
        worker = recovery_maintenance.run_queued_follow_up_maintenance
    elif blocked_operation == "goal_wakeups":
        monkeypatch.setattr(recovery_maintenance, "run_goal_wakeups", block)
        worker = recovery_maintenance.run_goal_wakeup_maintenance
    else:
        store.goals = SimpleNamespace(sweep_goal_scheduling=block)
        worker = recovery_maintenance.run_goal_scheduling_maintenance

    maintenance_task = asyncio.create_task(worker(store, interval_seconds=0.001))
    reaper_task = asyncio.create_task(attempts._run_attempt_reaper(store, interval_seconds=0.001))
    try:
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        await asyncio.wait_for(attempt_reaped.wait(), timeout=1)
    finally:
        maintenance_task.cancel()
        reaper_task.cancel()
        await asyncio.gather(maintenance_task, reaper_task, return_exceptions=True)

    assert operation_cancelled.is_set()


async def test_timed_out_maintenance_retries_on_the_next_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timed_out = asyncio.Event()
    retried = asyncio.Event()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                timed_out.set()
        retried.set()

    caplog.set_level("WARNING", logger="cloud_agent.app")
    task = asyncio.create_task(
        recovery_maintenance._run_periodic_loop(
            "maintenance_probe",
            operation,
            interval_seconds=0.001,
            deadline_seconds=0.01,
        )
    )
    try:
        await asyncio.wait_for(retried.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert timed_out.is_set()
    assert any(
        getattr(record, "event", None) == "agent_maintenance_probe_timeout"
        for record in caplog.records
    )
