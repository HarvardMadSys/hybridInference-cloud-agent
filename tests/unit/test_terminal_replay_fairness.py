"""Slow hosts and cancelled passes cannot monopolize durable replay selection."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.workers import terminal_replay as replay


def _rows(count):
    return [
        {"id": f"job-{index:04d}", "updated_at": datetime(2026, 1, 1, tzinfo=UTC), "host": "bad"}
        for index in range(count)
    ]


def _store(rows):
    async def page(*, after, limit):
        return [row for row in rows if after is None or (row["updated_at"], row["id"]) > after][
            :limit
        ]

    return SimpleNamespace(sandbox=SimpleNamespace(terminal_resume_page=page))


async def test_failed_first_page_does_not_starve_the_next_page(monkeypatch):
    rows = _rows(101)
    attempted = []

    async def failed(_store, row):
        attempted.append(row["id"])
        await asyncio.sleep(0)

    monkeypatch.setattr(replay, "_resume", failed)
    cursor = replay.ReplayCursor()
    store = _store(rows)
    await replay._reconcile_settled_terminals(store, cursor=cursor)
    await replay._reconcile_settled_terminals(store, cursor=cursor)
    assert attempted == [row["id"] for row in rows]
    assert cursor.after is None, "the following pass wraps and retries failed markers"


async def test_stalled_prefix_progresses_and_other_hosts_do_not_wait_for_its_slots(monkeypatch):
    rows = _rows(101)
    rows[50]["host"] = "healthy-within-page"
    rows[100]["host"] = "healthy-next-page"
    attempted = set()
    active = Counter()
    peak = Counter()

    async def resume(_store, row):
        host = row["host"]
        attempted.add(row["id"])
        active[host] += 1
        peak[host] = max(peak[host], active[host])
        try:
            if host == "bad":
                await asyncio.Event().wait()
        finally:
            active[host] -= 1

    monkeypatch.setattr(replay, "_resume", resume)
    monkeypatch.setattr(replay, "REPLAY_PASS_DEADLINE_S", 0.01)
    cursor = replay.ReplayCursor()
    store = _store(rows)
    await replay._reconcile_settled_terminals(store, cursor=cursor)
    assert rows[50]["id"] in attempted, "a blocked host must not consume all global slots"
    assert sum(active.values()) == 0, "deadline cancellation joins owned RPC work"
    for _ in range(50):
        await replay._reconcile_settled_terminals(store, cursor=cursor)
    assert attempted == {row["id"] for row in rows}
    assert peak["bad"] == replay.REPLAY_HOST_CONCURRENCY
    assert sum(active.values()) == 0


async def test_global_bound_and_parent_cancellation_join_all_children(monkeypatch):
    rows = _rows(100)
    for index, row in enumerate(rows):
        row["host"] = f"host-{index}"
    active = 0
    peak = 0
    filled = asyncio.Event()

    async def resume(_store, _row):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if active == replay.REPLAY_CONCURRENCY:
            filled.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(replay, "_resume", resume)
    task = asyncio.create_task(replay._reconcile_settled_terminals(_store(rows)))
    await asyncio.wait_for(filled.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0
    assert peak == replay.REPLAY_CONCURRENCY


async def test_deadline_also_cancels_candidate_query(monkeypatch):
    cancelled = asyncio.Event()

    async def page(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(replay, "REPLAY_PASS_DEADLINE_S", 0.01)
    store = SimpleNamespace(
        sandbox=SimpleNamespace(terminal_resume_page=AsyncMock(side_effect=page))
    )
    await replay._reconcile_settled_terminals(store)
    assert cancelled.is_set()
