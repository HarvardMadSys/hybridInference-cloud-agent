"""Cleanup must return during a gateway outage and own all of its workers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.application import grant_revocation
from cloud_agent.workers.attempts import _reap_lapsed_residencies, _revoke_dead_grants


async def test_revocations_share_a_bounded_worker_pool(monkeypatch):
    monkeypatch.setattr(grant_revocation, "REVOCATION_CONCURRENCY", 3)
    active = peak = 0
    seen = []

    async def revoke(grant_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        seen.append(grant_id)
        return True

    ids = [str(i) for i in range(200)]
    await grant_revocation.revoke_detached_grants(SimpleNamespace(revoke=revoke), ids)
    assert peak == 3
    assert sorted(seen) == sorted(ids)
    assert active == 0


@pytest.mark.parametrize("kind", ["attempt", "residency"])
async def test_deadline_abandons_detached_work_and_joins_workers(monkeypatch, kind, caplog):
    monkeypatch.setattr(grant_revocation, "REVOCATION_CONCURRENCY", 2)
    monkeypatch.setattr(grant_revocation, "REVOCATION_DEADLINE_S", 0.02)
    active = 0
    started = []
    detached = False

    async def take():
        nonlocal detached
        assert not detached
        detached = True
        return [{"grant_id": str(i), "thread_id": str(i)} for i in range(200)]

    async def revoke(grant_id):
        nonlocal active
        assert detached
        started.append(grant_id)
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    grants = SimpleNamespace(revoke=revoke)
    store = SimpleNamespace(take_revocable_grants=take, reap_expired_residencies=take)
    operation = _revoke_dead_grants if kind == "attempt" else _reap_lapsed_residencies
    await asyncio.wait_for(operation(store, grants), timeout=1)
    assert started == ["0", "1"]
    assert active == 0
    assert "agent_grant_revocation_pass_timed_out" in caplog.text


async def test_one_refused_revocation_does_not_skip_remaining_work():
    revoke = AsyncMock(side_effect=[RuntimeError("unavailable"), False, True])
    await grant_revocation.revoke_detached_grants(SimpleNamespace(revoke=revoke), ["a", "b", "c"])
    assert revoke.await_count == 3


async def test_parent_cancellation_joins_revocation_workers():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def revoke(_grant_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(
        grant_revocation.revoke_detached_grants(SimpleNamespace(revoke=revoke), ["a"])
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
