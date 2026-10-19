"""A cancelled queue sweep resumes without skipping the rest of its page."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent.application import pending_messages
from cloud_agent.application.pending_messages import QueueSweep, settle_waiting_queues


class _SweepRepository:
    def __init__(self, thread_ids: list[str]) -> None:
        self.thread_ids = thread_ids
        self.after_values: list[str | None] = []

    async def threads_to_settle(self, *, limit: int, after: str | None) -> list[str]:
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
    assert (
        await settle_waiting_queues(store, gateway=None, github_app=None, limit=3, sweep=sweep) == 2
    )
    assert visited == ["thread-a", "thread-b", "thread-b", "thread-c"]
    assert repository.after_values == [None, "thread-a"]
    assert sweep.after is None
