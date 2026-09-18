"""The capacity retention loop: bounded passes, and failures that stay local.

The points themselves are no longer written here — the heartbeat that accepts
an observation keeps it, which is what closed the gaps this loop used to leave
in the chart. What is pinned here is the rest: a pass that raises must not end
the task, because a task that ended would stop bounding a table that gains a
row per host per minute, and nothing would say so until the disk did.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cloud_agent.capacity_history import _prune, prune_host_capacity_history
from tests.support.storage import AllCapabilities


class FakeStore(AllCapabilities):
    """A capacity store that records its prunes and can be told to fail."""

    def __init__(self, *, error: Exception | None = None, removed: int = 0) -> None:
        """Start empty, failing with ``error`` when one is given."""
        self.pruned: list[tuple[float, int]] = []
        self.error = error
        self.removed = removed

    async def prune_capacity_history(
        self, *, older_than_seconds: float = 86400.0, limit: int = 2000
    ) -> int:
        """Record the call, then answer however this fake was configured."""
        self.pruned.append((older_than_seconds, limit))
        if self.error is not None:
            raise self.error
        return self.removed


class HangingStore(AllCapabilities):
    """A capacity store whose delete never returns."""

    def __init__(self) -> None:
        """Count the calls that were started."""
        self.calls = 0

    async def prune_capacity_history(self, **_: Any) -> int:
        """Never answer, so the caller's timeout is the only way out."""
        self.calls += 1
        await asyncio.Event().wait()
        return 0


async def test_one_pass_deletes_one_bounded_batch_of_the_retention_window() -> None:
    store = FakeStore(removed=17)

    await _prune(store, limit=17, timeout_seconds=1.0)

    assert store.pruned == [(86400.0, 17)]


async def test_a_failed_prune_is_contained_rather_than_raised() -> None:
    """A full disk or a lock conflict must not end the task that bounds growth."""
    store = FakeStore(error=RuntimeError("out of disk"))

    await _prune(store, limit=10, timeout_seconds=1.0)

    assert store.pruned == [(86400.0, 10)]


async def test_a_delete_that_hangs_is_given_up_on_rather_than_waited_out() -> None:
    store = HangingStore()

    await _prune(store, limit=10, timeout_seconds=0.01)

    assert store.calls == 1


async def test_the_loop_keeps_passes_apart_and_can_be_cancelled() -> None:
    store = FakeStore()
    task = asyncio.create_task(prune_host_capacity_history(store, interval_seconds=0.01))

    for _ in range(100):
        if store.pruned:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.pruned


async def test_a_failing_pass_does_not_end_the_loop() -> None:
    """The task dying would stop bounding the table with nothing to say so."""
    store = FakeStore(error=RuntimeError("first pass fails"))
    task = asyncio.create_task(prune_host_capacity_history(store, interval_seconds=0.01))

    for _ in range(200):
        if len(store.pruned) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.pruned) >= 2


async def test_a_non_positive_interval_is_refused() -> None:
    with pytest.raises(ValueError):
        await prune_host_capacity_history(FakeStore(), interval_seconds=0)
