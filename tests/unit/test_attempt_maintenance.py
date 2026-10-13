"""Attempt-maintenance scheduling must preserve live grant authorization."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cloud_agent.workers import attempts


class _HostPool:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def reap_offline_hosts(self, **_kwargs) -> list[object]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _GrantRepository:
    def __init__(self, *, block_renewable: bool = False) -> None:
        self.block_renewable = block_renewable
        self.renewal_started = asyncio.Event()
        self.renewal_cancelled = asyncio.Event()

    async def list_renewable_grants(self) -> list[dict[str, Any]]:
        if self.block_renewable:
            self.renewal_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.renewal_cancelled.set()
                raise
        return [
            {
                "grant_id": "agr_running",
                "job_id": "job_running",
                "attempt_id": "attempt_running",
                "lease_generation": 7,
            }
        ]

    async def take_revocable_grants(self) -> list[dict[str, Any]]:
        return []


class _AttemptCommands:
    async def sweep_queued_follow_ups(self) -> None:
        return None

    async def reap_expired(self, *, max_attempts: int) -> list[dict[str, Any]]:
        return []


class _PendingMessages:
    async def recoverable_send_now_handoffs(self, **_kwargs) -> list[dict[str, Any]]:
        return []

    async def expired_send_now_handoffs(self, **_kwargs) -> list[dict[str, Any]]:
        return []

    async def threads_to_settle(self, *, limit: int = 50) -> list[str]:
        return []


class _Store:
    def __init__(self, *, block_renewable: bool = False) -> None:
        self.host_pool = _HostPool()
        self.grants = _GrantRepository(block_renewable=block_renewable)
        self.attempt_commands = _AttemptCommands()
        self.pending_messages = _PendingMessages()
        self.residency = _Residency()
        self.goals = _Goals()
        self.goal_wakeups = _GoalWakeups()
        self.snapshot = _Snapshot()
        self.session_fork = _SessionFork()
        self.attachments = _Attachments()
        self.sandbox = _Sandbox()


class _Residency:
    async def reap_expired_residencies(self) -> list[dict[str, Any]]:
        return []


class _Goals:
    async def sweep_goal_scheduling(self) -> dict[str, list[object]]:
        return {"rearmed": [], "stalled": []}


class _GoalWakeups:
    async def claim_wakeups(self, *, limit: int) -> list[dict[str, Any]]:
        return []


class _Snapshot:
    async def reap_stalled_sandbox_snapshot_captures(self) -> list[object]:
        return []

    async def reap_stalled_snapshot_publishes(self) -> list[object]:
        return []


class _SessionFork:
    async def reap_stalled_session_forks(self) -> list[dict[str, Any]]:
        return []


class _Attachments:
    async def purge_stale_staged(self, *, older_than_hours: int) -> int:
        return 0


class _Sandbox:
    async def list_terminal_resumes_pending(self) -> list[str]:
        return []


class _GrantClient:
    def __init__(self, *, delay: float = 0.02) -> None:
        self.renewal_times: list[float] = []
        self.second_renewal = asyncio.Event()
        self.delay = delay
        self.active = 0
        self.maximum_active = 0

    async def renew(self, _grant_id: str) -> None:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.renewal_times.append(asyncio.get_running_loop().time())
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        if len(self.renewal_times) == 2:
            self.second_renewal.set()


async def test_grant_renewal_keeps_cadence_while_host_reaper_is_blocked():
    """An unrelated host-reaping delay must not consume a running grant's TTL."""
    store = _Store()
    grants = _GrantClient()
    task = asyncio.create_task(
        attempts.reap_expired_attempts(store, interval_seconds=0.01, grants=grants)
    )
    try:
        await asyncio.wait_for(store.host_pool.started.wait(), timeout=0.2)
        await asyncio.wait_for(grants.second_renewal.wait(), timeout=0.2)
        assert grants.renewal_times[1] - grants.renewal_times[0] < 0.1
        assert grants.maximum_active == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert store.host_pool.cancelled.is_set()


async def test_shutdown_cancels_and_joins_an_inflight_grant_renewal():
    """The lifespan owner cancels and joins both its maintenance sibling loops."""
    store = _Store(block_renewable=True)
    task = asyncio.create_task(
        attempts.reap_expired_attempts(store, interval_seconds=0.01, grants=_GrantClient(delay=0))
    )
    await asyncio.wait_for(store.host_pool.started.wait(), timeout=0.2)
    await asyncio.wait_for(store.grants.renewal_started.wait(), timeout=0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.host_pool.cancelled.is_set()
    assert store.grants.renewal_cancelled.is_set()
