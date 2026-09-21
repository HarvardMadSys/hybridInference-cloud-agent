"""The bounded write queue: every bound, and what each one counts.

The queue exists so that diagnostics cannot slow the business path, which
means its *limits* are the feature -- an unbounded queue absorbs congestion by
growing until the service stops answering, which is the failure this is here to
prevent. Each test pins one bound and the count it produces.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cloud_agent import measurement_writer
from cloud_agent.measurement_writer import MeasurementWriter, record_size


def _payload(size: int = 10) -> dict[str, Any]:
    """A record-shaped payload of roughly ``size`` bytes."""
    return {"measurement_id": "msm_1", "padding": "x" * size}


async def test_a_full_queue_drops_and_counts_rather_than_growing():
    """256 waiting records is the bound; the 257th is dropped, not queued."""
    writer = MeasurementWriter(max_count=3)

    async def _persist() -> str:
        return "stored"

    writer.start()
    try:
        # No worker runs until the loop yields, so these all queue.
        accepted = [writer.offer(persist=_persist, payload=_payload()) for _ in range(3)]
        refused = writer.offer(persist=_persist, payload=_payload())

        assert accepted == [True, True, True]
        assert refused is False
        assert writer.counts.dropped_full == 1
        assert writer.counts.reasons["queue_count"] == 1
    finally:
        await writer.shutdown()


async def test_the_byte_budget_bounds_records_the_count_would_allow():
    """A few large records must not hold more than the byte budget.

    Offered without yielding, so no worker has taken one yet and all the bytes
    are still queued. Room for one record but not two is exactly the case a
    count-only bound would miss.
    """
    one = record_size(_payload(100))
    writer = MeasurementWriter(max_count=1000, max_bytes=one + 1)

    async def _persist() -> str:
        return "stored"

    writer.start()
    try:
        assert writer.offer(persist=_persist, payload=_payload(100)) is True
        assert writer.offer(persist=_persist, payload=_payload(100)) is False

        assert writer.counts.dropped_full == 1
        assert writer.counts.reasons["queue_bytes"] == 1
    finally:
        await writer.shutdown()


async def test_an_accepted_record_is_written_off_the_calling_path():
    """Offering returns before the write happens."""
    writer = MeasurementWriter()
    written: list[str] = []

    async def _persist() -> str:
        written.append("done")
        return "stored"

    writer.start()
    try:
        assert writer.offer(persist=_persist, payload=_payload()) is True
        assert written == [], "the caller must not wait for the write"
        for _ in range(50):
            if written:
                break
            await asyncio.sleep(0.02)
        assert written == ["done"]
        assert writer.counts.stored == 1
    finally:
        await writer.shutdown()


async def test_a_write_that_outlives_its_timeout_is_dropped_and_counted():
    """A slow destination must not hold a worker, or a pooled connection."""
    writer = MeasurementWriter(timeout_s=0.05)

    async def _never() -> str:
        await asyncio.sleep(30)
        return "stored"

    writer.start()
    try:
        writer.offer(persist=_never, payload=_payload())
        for _ in range(50):
            if writer.counts.dropped_timeout:
                break
            await asyncio.sleep(0.02)
        assert writer.counts.dropped_timeout == 1
        assert writer.counts.stored == 0
    finally:
        await writer.shutdown()


async def test_a_failing_write_is_counted_by_exception_name():
    """A store that raises is counted, and never reaches the request."""
    writer = MeasurementWriter()

    async def _boom() -> str:
        raise RuntimeError("pool exhausted")

    writer.start()
    try:
        writer.offer(persist=_boom, payload=_payload())
        for _ in range(50):
            if writer.counts.failed:
                break
            await asyncio.sleep(0.02)
        assert writer.counts.failed == 1
        assert writer.counts.reasons["RuntimeError"] == 1
    finally:
        await writer.shutdown()


@pytest.mark.parametrize(
    ("result", "field"),
    [
        ("stored", "stored"),
        ("duplicate", "duplicate"),
        ("rejected", "rejected"),
        ("conflict", "conflict"),
        ("run_conflict", "conflict"),
    ],
)
async def test_each_store_outcome_lands_in_its_own_counter(result: str, field: str):
    """A refusal is not a store: the counters have to say which happened."""
    writer = MeasurementWriter()

    async def _persist() -> str:
        return result

    writer.start()
    try:
        writer.offer(persist=_persist, payload=_payload())
        for _ in range(50):
            if getattr(writer.counts, field):
                break
            await asyncio.sleep(0.02)
        assert getattr(writer.counts, field) == 1
        assert writer.counts.failed == 0
    finally:
        await writer.shutdown()


async def test_a_shutdown_finishes_the_queue_within_its_bound():
    """Stopping waits for what is queued, but only briefly."""
    writer = MeasurementWriter()
    written: list[str] = []

    async def _persist() -> str:
        written.append("done")
        return "stored"

    writer.start()
    try:
        for _ in range(5):
            writer.offer(persist=_persist, payload=_payload())
        counts = await writer.shutdown(timeout_s=2.0)

        assert len(written) == 5, "the queue must be finished, not discarded"
        assert counts.dropped_shutdown == 0
    finally:
        await writer.shutdown()


async def test_a_shutdown_that_cannot_finish_counts_what_it_abandoned():
    """A destination that is not answering must not hold the stop open."""
    writer = MeasurementWriter()

    async def _never() -> str:
        await asyncio.sleep(30)
        return "stored"

    writer.start()
    try:
        for _ in range(4):
            writer.offer(persist=_never, payload=_payload())
        counts = await writer.shutdown(timeout_s=0.1)

        # Counted, not silently forgotten: an operator can see the loss.
        assert counts.dropped_shutdown + counts.dropped_timeout >= 1
    finally:
        await writer.shutdown()


async def test_shutdown_counts_a_write_already_taken_by_a_worker():
    """An in-flight write is not invisible merely because it left the queue."""
    writer = MeasurementWriter()
    started = asyncio.Event()

    async def _never() -> str:
        started.set()
        await asyncio.sleep(30)
        return "stored"

    writer.start()
    try:
        assert writer.offer(persist=_never, payload=_payload()) is True
        await asyncio.wait_for(started.wait(), timeout=1)
        assert writer.pending == 0
        assert writer.in_flight == 1

        counts = await writer.shutdown(timeout_s=0.05)

        assert counts.dropped_shutdown == 1
        assert counts.stored == 0
        assert writer.in_flight == 0
    finally:
        await writer.shutdown()


async def test_the_count_bound_includes_writes_already_in_flight():
    """Workers taking rows may not make room beyond the configured bound."""
    writer = MeasurementWriter(max_count=2, max_concurrent=2)
    started = asyncio.Event()
    release = asyncio.Event()
    entered = 0

    async def _blocked() -> str:
        nonlocal entered
        entered += 1
        if entered == 2:
            started.set()
        await release.wait()
        return "stored"

    writer.start()
    try:
        assert writer.offer(persist=_blocked, payload=_payload()) is True
        assert writer.offer(persist=_blocked, payload=_payload()) is True
        await asyncio.wait_for(started.wait(), timeout=1)
        assert writer.pending == 0
        assert writer.in_flight == 2
        assert writer.offer(persist=_blocked, payload=_payload()) is False
        assert writer.counts.dropped_full == 1
    finally:
        release.set()
        await writer.shutdown()


async def test_offering_after_shutdown_is_counted_not_raised():
    """Between a stop and the next start there is nowhere for a record to go."""
    writer = MeasurementWriter()
    writer.start()
    await writer.shutdown()

    assert writer.offer(persist=lambda: None, payload=_payload()) is False
    assert writer.counts.dropped_shutdown >= 1
    assert writer.counts.reasons["writer_stopped"] == 1


async def test_concurrency_stays_within_the_pool():
    """Diagnostic writes may not take the whole connection pool."""
    writer = MeasurementWriter(max_concurrent=2)
    running = 0
    peak = 0

    async def _persist() -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return "stored"

    writer.start()
    try:
        for _ in range(8):
            writer.offer(persist=_persist, payload=_payload())
        for _ in range(100):
            if writer.counts.stored >= 8:
                break
            await asyncio.sleep(0.02)
        assert writer.counts.stored == 8
        assert peak <= 2, f"pool of 2 exceeded: peak {peak}"
    finally:
        await writer.shutdown()


async def test_the_process_queue_is_one_shared_object():
    """One destination, one queue: two callers must share its bounds."""
    measurement_writer.reset_writer(None)
    try:
        assert measurement_writer.writer() is measurement_writer.writer()
    finally:
        measurement_writer.reset_writer(None)
