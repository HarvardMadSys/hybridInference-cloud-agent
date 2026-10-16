"""The durable send-now consumer stays bounded beside the attempt reaper."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_agent.application import send_now
from cloud_agent.workers import attempts


class _Pending:
    def __init__(self, count: int) -> None:
        self.candidates = [
            {"message_id": f"apmsg_{index}", "revision": 2} for index in range(count)
        ]
        self.expired_called = False

    async def recoverable_send_now_handoffs(self, **_kwargs):
        return list(self.candidates)

    async def expired_send_now_handoffs(self, **_kwargs):
        self.expired_called = True
        return []


async def test_recovery_bounds_concurrent_sandbox_manager_calls(monkeypatch):
    pending = _Pending(7)
    store = SimpleNamespace(pending_messages=pending)
    active = maximum = 0

    async def deliver(_store, *, message_id, revision):
        nonlocal active, maximum
        assert message_id.startswith("apmsg_") and revision == 2
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return send_now.HandoffOutcome(claimed=True, accepted=True)

    monkeypatch.setattr(send_now, "HAND_OVER_RECOVERY_CONCURRENCY", 2)
    monkeypatch.setattr(send_now, "HAND_OVER_RECOVERY_DEADLINE_S", 1.0)
    monkeypatch.setattr(send_now, "deliver_handoff", deliver)

    result = await send_now.recover_handoffs(store)  # type: ignore[arg-type]

    assert result == {"claimed": 7, "accepted": 7, "requeued": 0}
    assert maximum == 2
    assert pending.expired_called


async def test_recovery_deadline_cancels_sandbox_manager_calls_before_the_next_pass(monkeypatch):
    pending = _Pending(3)
    store = SimpleNamespace(pending_messages=pending)
    cancelled = 0

    async def deliver(_store, *, message_id, revision):
        nonlocal cancelled
        assert message_id.startswith("apmsg_") and revision == 2
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    monkeypatch.setattr(send_now, "HAND_OVER_RECOVERY_CONCURRENCY", 2)
    monkeypatch.setattr(send_now, "HAND_OVER_RECOVERY_DEADLINE_S", 0.01)
    monkeypatch.setattr(send_now, "deliver_handoff", deliver)

    result = await asyncio.wait_for(
        send_now.recover_handoffs(store),  # type: ignore[arg-type]
        1,
    )

    assert result == {"claimed": 0, "accepted": 0, "requeued": 0}
    assert cancelled == 2
    assert pending.expired_called


async def test_handoff_recovery_runs_at_startup_and_each_pass(monkeypatch):
    recovered_twice = asyncio.Event()
    recoveries = 0

    async def recover(_store):
        nonlocal recoveries
        recoveries += 1
        if recoveries == 2:
            recovered_twice.set()

    monkeypatch.setattr(attempts, "recover_handoffs", recover)
    task = asyncio.create_task(
        attempts._recover_send_now_handoffs(
            SimpleNamespace(),  # type: ignore[arg-type]
            interval_seconds=0.001,
        )
    )
    try:
        await asyncio.wait_for(recovered_twice.wait(), 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert recoveries == 2


async def test_hung_handoff_recovery_does_not_delay_attempt_reaping(monkeypatch):
    recovery_started = asyncio.Event()
    recovery_cancelled = asyncio.Event()
    attempt_reaped = asyncio.Event()

    async def recover(_store):
        recovery_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            recovery_cancelled.set()

    async def reap_expired(*, max_attempts):
        assert max_attempts == attempts.MAX_ATTEMPTS
        attempt_reaped.set()
        return []

    monkeypatch.setattr(attempts, "recover_handoffs", recover)
    monkeypatch.setattr(attempts, "_reconcile_settled_terminals", AsyncMock())
    monkeypatch.setattr(attempts, "_reap_offline_hosts", AsyncMock())
    monkeypatch.setattr(attempts, "_reap_lapsed_residencies", AsyncMock())
    monkeypatch.setattr(attempts, "_cleanup_reaped_shared_residencies", AsyncMock())
    monkeypatch.setattr(attempts, "settle_waiting_queues", AsyncMock())
    store = SimpleNamespace(
        host_pool=object(),
        residency=object(),
        attachments=SimpleNamespace(purge_stale_staged=AsyncMock(return_value=0)),
        attempt_commands=SimpleNamespace(
            reap_expired=reap_expired,
        ),
        snapshot=SimpleNamespace(
            reap_stalled_sandbox_snapshot_captures=AsyncMock(return_value=[]),
            reap_stalled_snapshot_publishes=AsyncMock(return_value=[]),
        ),
        session_fork=SimpleNamespace(reap_stalled_session_forks=AsyncMock(return_value=[])),
    )
    task = asyncio.create_task(
        attempts.reap_expired_attempts(  # type: ignore[arg-type]
            store,
            interval_seconds=0.001,
        )
    )
    try:
        await asyncio.wait_for(recovery_started.wait(), 1)
        await asyncio.wait_for(attempt_reaped.wait(), 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert recovery_cancelled.is_set()
