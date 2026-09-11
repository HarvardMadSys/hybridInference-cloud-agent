"""The capacity history loop: bounded rounds, resumable cursors, isolation.

The properties here are the ones an operator never sees until they matter. A
round that overruns its budget delays the next one; a cursor that restarts from
the first host starves every host behind a slow one; a write failure that
propagates ends the task and silently stops the chart. Each of those looks like
"the graph has a gap" from outside, which is exactly why they are pinned here
rather than left to the database tier.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from cloud_agent.capacity_history import (
    HISTORY_BATCH_SIZE,
    _record_round,
    _remaining,
    collect_host_capacity_history,
)
from cloud_agent.capacity_protocol import bucket_for
from tests.support.storage import AllCapabilities

SAMPLE = "00000000-0000-4000-8000-000000000001"
EPOCH = "00000000-0000-4000-8000-0000000000ff"
BOOT = "00000000-0000-4000-8000-000000000002"


class TickingClock:
    """A monotonic clock that advances once per database call."""

    def __init__(self, *, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += self.step


def candidate(host: str, *, sampled_at: datetime | None = None, **overrides: Any) -> dict:
    """One store-projected candidate row, healthy unless overridden."""
    when = sampled_at or datetime(2026, 9, 11, 12, 34, 56, tzinfo=UTC)
    row: dict[str, Any] = {
        "host": host,
        "capacity_epoch": EPOCH,
        "capacity_sample_id": SAMPLE,
        "capacity_sampled_at": when,
        "linux_boot_id": BOOT,
        "cpu_percent": "37.2",
        "cpu_logical_count": "32",
        "cpu_window_seconds": "30.01",
        "cpu_missing_reason": None,
        "memory_total_bytes": "1024",
        "memory_available_bytes": "512",
        "memory_missing_reason": None,
    }
    row.update(overrides)
    return row


class FakeStore(AllCapabilities):
    """The store surface the task uses, with per-call failure injection."""

    def __init__(
        self,
        rows: list[dict],
        *,
        clock: TickingClock | None = None,
        read_error: Exception | None = None,
        prune_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.rows = sorted(rows, key=lambda row: row["host"])
        self.clock = clock
        self.read_error = read_error
        self.prune_error = prune_error
        self.write_error = write_error
        self.reads: list[str | None] = []
        self.written: list[tuple[str, Any, Any]] = []
        self.pruned: list[tuple[float, int]] = []

    async def list_capacity_history_candidates(
        self, *, after_host: str | None = None, limit: int = HISTORY_BATCH_SIZE
    ) -> list[dict]:
        if self.clock is not None:
            self.clock.tick()
        self.reads.append(after_host)
        if self.read_error is not None:
            raise self.read_error
        remaining = [row for row in self.rows if after_host is None or row["host"] > after_host]
        return remaining[:limit]

    async def record_capacity_history_point(
        self, *, host: str, capacity_epoch: Any, point: Any
    ) -> bool:
        if self.write_error is not None:
            raise self.write_error
        self.written.append((host, capacity_epoch, point))
        return True

    async def prune_capacity_history(self, *, older_than_seconds: float, limit: int) -> int:
        if self.prune_error is not None:
            raise self.prune_error
        self.pruned.append((older_than_seconds, limit))
        return 0


async def run_round(store: FakeStore, cursor: str | None, **overrides: Any) -> str | None:
    """One round with the test's own clock, so no real time passes."""
    options: dict[str, Any] = {
        "batch_size": HISTORY_BATCH_SIZE,
        "batch_timeout_seconds": 2.0,
        "round_budget_seconds": 10.0,
        "prune_batch_size": 2000,
        "clock": store.clock or (lambda: 0.0),
    }
    options.update(overrides)
    return await _record_round(store, cursor, **options)


# ── One round ─────────────────────────────────────────────────────────


async def test_one_round_writes_one_point_per_fresh_host() -> None:
    clock = TickingClock(step=0.0)
    store = FakeStore([candidate("alpha"), candidate("beta")], clock=clock)
    cursor = await run_round(store, None)
    assert cursor is None
    assert [host for host, _, _ in store.written] == ["alpha", "beta"]
    assert store.written[0][1] == EPOCH
    assert store.written[0][2].bucket_at == bucket_for(
        datetime(2026, 9, 11, 12, 34, 56, tzinfo=UTC)
    )


async def test_the_epoch_written_is_the_one_that_was_read() -> None:
    """A delayed write must never stamp an old sample with the newest epoch."""
    store = FakeStore([candidate("alpha")])
    await run_round(store, None)
    assert store.written[0][1] == EPOCH


async def test_an_empty_pool_resets_the_cursor() -> None:
    store = FakeStore([])
    assert await run_round(store, "somewhere-in-the-middle") is None


# ── The budget and the cursor ─────────────────────────────────────────


async def test_a_truncated_round_resumes_where_it_stopped() -> None:
    """Restarting from the first host would starve everything behind it."""
    clock = TickingClock(step=1.0)
    store = FakeStore([candidate(f"host-{index}") for index in range(5)], clock=clock)
    cursor = await run_round(store, None, batch_size=1, round_budget_seconds=3.0)
    assert [host for host, _, _ in store.written] == ["host-0", "host-1"]
    assert cursor == "host-1"

    clock.step = 0.0
    resumed = await run_round(store, cursor, batch_size=1, round_budget_seconds=100.0)
    assert resumed is None
    assert [host for host, _, _ in store.written][2:] == ["host-2", "host-3", "host-4"]


async def test_the_budget_is_checked_before_every_write_not_only_every_page() -> None:
    """A page is a hundred hosts, and each write has a timeout of its own.

    Checking the clock at the page boundary only means one slow page can run a
    hundred timeouts long — the round overruns the budget it was given while
    still reporting that it finished, and the next round starts late with no
    cursor to show for the work.
    """
    budget_seconds, seconds_per_write = 10.0, 1.0
    clock = TickingClock(step=1.0)
    store = FakeStore([candidate(f"host-{index:03d}") for index in range(100)], clock=clock)
    original = store.record_capacity_history_point

    async def slow_write(*, host: str, capacity_epoch: Any, point: Any) -> bool:
        clock.tick()
        return await original(host=host, capacity_epoch=capacity_epoch, point=point)

    store.record_capacity_history_point = slow_write  # type: ignore[method-assign]

    cursor = await run_round(store, None, batch_size=100, round_budget_seconds=budget_seconds)

    assert len(store.written) <= budget_seconds / seconds_per_write
    # The cursor names the last host actually written, so the next round
    # resumes after it rather than re-measuring the whole page.
    assert cursor == store.written[-1][0]

    clock.step = 0.0
    resumed = await run_round(store, cursor, batch_size=100, round_budget_seconds=100.0)
    assert resumed is None
    written = [host for host, _, _ in store.written]
    assert len(written) == len(set(written)) == 100


async def test_a_completed_round_starts_over_so_new_hosts_are_seen() -> None:
    store = FakeStore([candidate("alpha")])
    assert await run_round(store, "alpha", batch_size=8) is None
    assert store.reads == ["alpha"]


async def test_a_full_page_keeps_paging_until_the_pool_is_walked() -> None:
    store = FakeStore([candidate(f"host-{index}") for index in range(5)])
    assert await run_round(store, None, batch_size=2) is None
    assert store.reads == [None, "host-1", "host-3"]
    assert len(store.written) == 5


def test_remaining_clamps_an_operation_to_what_the_round_has_left() -> None:
    """The budget has to bound the operation, not merely gate its start."""
    assert _remaining(10.0, lambda: 0.0, 2.0) == 2.0
    assert _remaining(10.0, lambda: 8.0, 2.0) == 2.0
    assert _remaining(10.0, lambda: 9.0, 2.0) == 1.0
    assert _remaining(10.0, lambda: 10.0, 2.0) == 0.0
    assert _remaining(10.0, lambda: 12.0, 2.0) == 0.0


async def test_the_round_budget_bounds_the_last_operation_too() -> None:
    """A soft budget is not a budget.

    Gating each operation on "is there time left" still lets the one that
    starts with a millisecond to spare run its whole timeout, so a round
    configured for ten seconds finishes in twelve. Here the operation never
    completes on its own: only the round's own deadline can end it, and the
    elapsed wall clock is what says whether the deadline or the timeout won.
    """
    budget_seconds, operation_ceiling = 0.2, 2.0
    store = FakeStore([candidate("alpha"), candidate("beta")])

    attempted: list[str] = []

    class BlockingStore(FakeStore):
        async def record_capacity_history_point(self, *, host: str, **_kwargs: Any) -> bool:
            attempted.append(host)
            await asyncio.sleep(3600)
            return True  # pragma: no cover - only reached without a deadline

    blocking = BlockingStore(store.rows)
    started = time.monotonic()
    cursor = await run_round(
        blocking,
        None,
        batch_timeout_seconds=operation_ceiling,
        round_budget_seconds=budget_seconds,
        clock=time.monotonic,
    )
    elapsed = time.monotonic() - started

    # Unclamped this waits the full two-second ceiling before giving up.
    assert elapsed < budget_seconds * 2
    # It stopped at the host it could not finish rather than starting another
    # one it could not finish either, and the next round resumes after it.
    assert attempted == ["alpha"]
    assert cursor == "alpha"


# ── Failure isolation ─────────────────────────────────────────────────


async def test_a_failed_read_ends_the_round_without_raising() -> None:
    store = FakeStore([candidate("alpha")], read_error=RuntimeError("database is down"))
    assert await run_round(store, None) is None
    assert store.written == []


async def test_a_failed_read_keeps_the_cursor_for_the_next_round() -> None:
    store = FakeStore([candidate("alpha")], read_error=RuntimeError("database is down"))
    assert await run_round(store, "host-9") == "host-9"


async def test_one_unusable_row_does_not_stop_the_others() -> None:
    store = FakeStore(
        [
            candidate("alpha", capacity_sample_id="not-a-uuid"),
            candidate("beta"),
        ]
    )
    await run_round(store, None)
    assert [host for host, _, _ in store.written] == ["beta"]


async def test_one_failed_write_does_not_stop_the_others() -> None:
    class OneBadWrite(FakeStore):
        async def record_capacity_history_point(
            self, *, host: str, capacity_epoch: Any, point: Any
        ):
            if host == "alpha":
                raise RuntimeError("deadlock detected")
            return await super().record_capacity_history_point(
                host=host, capacity_epoch=capacity_epoch, point=point
            )

    store = OneBadWrite([candidate("alpha"), candidate("beta")])
    await run_round(store, None)
    assert [host for host, _, _ in store.written] == ["beta"]


async def test_a_failed_prune_does_not_stop_recording() -> None:
    """Retention failing must not be a reason to stop measuring."""
    store = FakeStore([candidate("alpha")], prune_error=RuntimeError("out of disk"))
    await run_round(store, None)
    assert [host for host, _, _ in store.written] == ["alpha"]


async def test_the_prune_is_bounded_and_runs_every_round() -> None:
    store = FakeStore([candidate("alpha")])
    await run_round(store, None, prune_batch_size=17)
    assert store.pruned == [(86400.0, 17)]


# ── The loop ──────────────────────────────────────────────────────────


async def test_the_loop_keeps_rounds_apart_and_can_be_cancelled() -> None:
    store = FakeStore([candidate("alpha")])
    task = asyncio.create_task(
        collect_host_capacity_history(store, interval_seconds=0.01, clock=lambda: 0.0)
    )
    for _ in range(100):
        if store.written:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.written


async def test_a_non_positive_interval_is_refused() -> None:
    store = FakeStore([])
    with pytest.raises(ValueError):
        await collect_host_capacity_history(store, interval_seconds=0)


async def test_a_failing_round_does_not_end_the_loop() -> None:
    """The whole task dying would silently stop the chart for good."""
    store = FakeStore([candidate("alpha")], read_error=RuntimeError("first round fails"))
    task = asyncio.create_task(
        collect_host_capacity_history(
            store, interval_seconds=0.01, round_budget_seconds=10.0, clock=lambda: 0.0
        )
    )
    for _ in range(200):
        if len(store.reads) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.reads) >= 2
