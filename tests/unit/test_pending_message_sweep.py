"""A cancelled queue sweep resumes without skipping the rest of its page."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent.application import pending_messages
from cloud_agent.application.pending_messages import QueueSweep, settle_waiting_queues


class _SweepRepository:
    def __init__(self, thread_ids: list[str]) -> None:
        self.thread_ids = thread_ids
        self.after_values: list[str | None] = []

    async def settlement_sweep_boundary(self) -> datetime:
        return datetime(2026, 10, 16, tzinfo=timezone.utc)

    async def threads_to_settle(
        self, *, limit: int, after: str | None, created_before: datetime | None
    ) -> list[str]:
        assert created_before is not None
        self.after_values.append(after)
        return [thread_id for thread_id in self.thread_ids if after is None or thread_id > after][
            :limit
        ]


@pytest.mark.asyncio
async def test_cancellation_mid_page_keeps_cursor_at_last_completed_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _SweepRepository(["thread-a", "thread-b", "thread-c"])
    store = SimpleNamespace(pending_messages=repository)
    entered_second = asyncio.Event()
    visited: list[str] = []

    async def settle(_store: Any, *, thread_id: str, gateway: Any, app_credentials: Any) -> None:
        del gateway, app_credentials
        visited.append(thread_id)
        if thread_id == "thread-b" and not entered_second.is_set():
            entered_second.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(pending_messages, "_settle_with", settle)
    sweep = QueueSweep()
    task = asyncio.create_task(
        settle_waiting_queues(store, gateway=None, github_app=None, limit=3, sweep=sweep)
    )
    await asyncio.wait_for(entered_second.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sweep.after == "thread-a"
    assert sweep.created_before is not None
    assert (
        await settle_waiting_queues(store, gateway=None, github_app=None, limit=3, sweep=sweep) == 2
    )
    assert visited == ["thread-a", "thread-b", "thread-b", "thread-c"]
    assert repository.after_values == [None, "thread-a"]
    assert sweep.after is None
    assert sweep.created_before is None


@pytest.mark.asyncio
async def test_a_new_process_sweep_restarts_from_the_first_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _SweepRepository(["thread-a", "thread-b", "thread-c"])
    store = SimpleNamespace(pending_messages=repository)
    visited: list[str] = []

    async def settle(_store: Any, *, thread_id: str, gateway: Any, app_credentials: Any) -> None:
        del gateway, app_credentials
        visited.append(thread_id)

    monkeypatch.setattr(pending_messages, "_settle_with", settle)
    stale_process_sweep = QueueSweep(
        after="thread-b", created_before=datetime(2026, 10, 16, tzinfo=timezone.utc)
    )
    restarted_process_sweep = QueueSweep()

    await settle_waiting_queues(
        store,
        gateway=None,
        github_app=None,
        limit=3,
        sweep=restarted_process_sweep,
    )

    assert stale_process_sweep.after == "thread-b"
    assert repository.after_values == [None]
    assert visited == ["thread-a", "thread-b", "thread-c"]
