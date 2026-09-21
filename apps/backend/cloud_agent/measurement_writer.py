"""A bounded, counted write queue between a request and the diagnostics table.

The control plane roots file and terminal measurements in-process, so the
write happens in the same service that answers the user. Left unbounded, a
database that is slow turns every request into another pending write, and the
diagnostics that exist to observe the service become the reason it stops
answering -- the design's requirement is that control-plane congestion never
slows the business path.

So a record is *offered*, never awaited: the caller puts it on a queue bounded
by both a count and a total payload size, and the writes happen on a small
fixed pool of workers. When there is no room, or the pool is saturated, the
record is dropped and counted rather than queued -- a bounded loss an operator
can see beats an unbounded one they cannot.

Nothing here raises at a caller. Every failure is a count and a log line,
because the file was read and the terminal answered long before this runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

#: How many records may wait. The design reuses the existing default of 256
#: for the delivery queues, and this is the control plane's half of it.
MAX_QUEUED_MEASUREMENTS = 256

#: Total payload the queue may hold, in bytes. 4 MiB is the design's per
#: destination bound: a count alone lets a few 64 KiB records hold far more
#: than that, and a size alone lets thousands of tiny ones.
MAX_QUEUED_BYTES = 4 * 1024 * 1024

#: How many writes may be in flight at once. Small on purpose: this is
#: diagnostic traffic sharing a connection pool with business queries, so it
#: must not be able to take the pool.
MAX_CONCURRENT_WRITES = 2

#: How long one write may take before it is abandoned and counted. A write
#: that outlives this is holding a pooled connection the business path wants.
WRITE_TIMEOUT_S = 10.0

#: How long a shutdown may spend finishing what is already queued. Bounded
#: because a stop must not be held hostage by a destination that is not
#: answering; what is left is counted as dropped, not silently forgotten.
SHUTDOWN_DRAIN_S = 2.0


@dataclass
class WriteCounts:
    """What the queue has done, so an operator can see the loss.

    Deliberately not a promise of zero loss: a process that dies still loses
    whatever it was holding, and the design says so. These are the losses this
    process could observe and count.
    """

    stored: int = 0
    duplicate: int = 0
    rejected: int = 0
    conflict: int = 0
    dropped_full: int = 0
    dropped_timeout: int = 0
    dropped_shutdown: int = 0
    failed: int = 0
    #: Names of the reasons seen, and how often, so a log line can say *why*
    #: without carrying the record itself.
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, name: str) -> None:
        """Count one reason by name."""
        self.reasons[name] = self.reasons.get(name, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for the admin surface and logs."""
        return {
            "stored": self.stored,
            "duplicate": self.duplicate,
            "rejected": self.rejected,
            "conflict": self.conflict,
            "dropped_full": self.dropped_full,
            "dropped_timeout": self.dropped_timeout,
            "dropped_shutdown": self.dropped_shutdown,
            "failed": self.failed,
            "reasons": dict(self.reasons),
        }


@dataclass(frozen=True)
class _Pending:
    """One queued write, with the size it contributes to the byte bound."""

    persist: Any
    size: int


def record_size(payload: Mapping[str, Any]) -> int:
    """Return the bytes one queued record will cost the destination.

    Serialized rather than estimated from the number of fields: the bound is
    about what would be sent, and only the encoded form says that.
    """
    try:
        return len(json.dumps(payload, separators=(",", ":"), default=str).encode())
    except (TypeError, ValueError):
        return 0


class MeasurementWriter:
    """Offer records to a bounded queue, and write them off the request path."""

    def __init__(
        self,
        *,
        max_count: int = MAX_QUEUED_MEASUREMENTS,
        max_bytes: int = MAX_QUEUED_BYTES,
        max_concurrent: int = MAX_CONCURRENT_WRITES,
        timeout_s: float = WRITE_TIMEOUT_S,
    ) -> None:
        self.counts = WriteCounts()
        self._max_count = max_count
        self._max_bytes = max_bytes
        self._max_concurrent = max_concurrent
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._pending: list[_Pending] = []
        self._bytes = 0
        self._in_flight = 0
        self._in_flight_bytes = 0
        self._running = False
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workers: list[asyncio.Task[None]] = []

    # ── Offering ──────────────────────────────────────────────────────

    def offer(self, *, persist: Any, payload: Mapping[str, Any]) -> bool:
        """Queue one write, or count it as dropped when there is no room.

        Returns whether it was accepted. Never blocks and never raises: the
        caller is a request that has already been answered, or is about to be.
        """
        size = record_size(payload)
        with self._lock:
            if not self._running:
                # Between shutdown and the next start there is nowhere for this
                # to go, and counting it is more honest than pretending.
                self.counts.dropped_shutdown += 1
                self.counts.note("writer_stopped")
                return False
            accepted = len(self._pending) + self._in_flight
            accepted_bytes = self._bytes + self._in_flight_bytes
            if accepted >= self._max_count or accepted_bytes + size > self._max_bytes:
                # Which bound was hit is the useful part: a full count means
                # the destination is behind, a full byte budget means the
                # records are large.
                reason = "queue_count" if accepted >= self._max_count else "queue_bytes"
                self.counts.dropped_full += 1
                self.counts.note(reason)
                return False
            self._pending.append(_Pending(persist=persist, size=size))
            self._bytes += size
        _signal(self)
        return True

    @property
    def pending(self) -> int:
        """How many records are waiting, for tests and the admin surface."""
        with self._lock:
            return len(self._pending)

    @property
    def queued_bytes(self) -> int:
        """How many payload bytes are waiting."""
        with self._lock:
            return self._bytes

    @property
    def in_flight(self) -> int:
        """How many accepted writes currently hold a worker."""
        with self._lock:
            return self._in_flight

    @property
    def running(self) -> bool:
        """Whether this process currently accepts measurement writes."""
        with self._lock:
            return self._running

    # ── Lifetime ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the worker pool on the running loop. Idempotent."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._running:
                return
            self._running = True
        self._loop = loop
        self._wake = asyncio.Event()
        self._workers = [
            loop.create_task(self._work(index), name=f"measurement-writer-{index}")
            for index in range(self._max_concurrent)
        ]

    async def shutdown(self, timeout_s: float = SHUTDOWN_DRAIN_S) -> WriteCounts:
        """Stop accepting, finish what is queued within a bound, and report."""
        with self._lock:
            self._running = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            with self._lock:
                if not self._pending and self._in_flight == 0:
                    break
            _signal(self)
            await asyncio.sleep(0.02)
        with self._lock:
            left, self._pending = self._pending, []
            in_flight = self._in_flight
            self._bytes = 0
        abandoned = len(left) + in_flight
        if abandoned:
            self.counts.dropped_shutdown += abandoned
            self.counts.note("shutdown")
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._loop = None
        self._wake = None
        return self.counts

    async def _work(self, index: int) -> None:
        """Take one record at a time and write it, counting every outcome."""
        del index
        while True:
            item = self._take()
            if item is None:
                wake = self._wake
                if wake is None:
                    return
                wake.clear()
                # Cleared before the wait, so a record offered between the
                # take and the wait still wakes this worker.
                with self._lock:
                    if self._pending:
                        continue
                try:
                    await asyncio.wait_for(wake.wait(), timeout=1.0)
                except TimeoutError:
                    continue
                continue
            try:
                await self._write(item)
            finally:
                self._complete(item)

    def _take(self) -> _Pending | None:
        """Remove the oldest queued record, or return ``None`` when empty."""
        with self._lock:
            if not self._pending:
                return None
            item = self._pending.pop(0)
            self._bytes -= item.size
            self._in_flight += 1
            self._in_flight_bytes += item.size
            return item

    def _complete(self, item: _Pending) -> None:
        """Release one worker-held record after success, failure, or cancellation."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._in_flight_bytes = max(0, self._in_flight_bytes - item.size)

    async def _write(self, item: _Pending) -> None:
        """Run one persist call under the write timeout, counting its result."""
        try:
            result = await asyncio.wait_for(item.persist(), timeout=self._timeout_s)
        except TimeoutError:
            self.counts.dropped_timeout += 1
            self.counts.note("write_timeout")
            logger.warning(
                "operation_measurement_write_timeout",
                extra={
                    "event": "operation_measurement_write_timeout",
                    "timeout_s": self._timeout_s,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.counts.failed += 1
            self.counts.note(type(exc).__name__)
            logger.warning(
                "operation_measurement_write_failed",
                exc_info=True,
                extra={"event": "operation_measurement_write_failed"},
            )
        else:
            self._count_result(str(result))

    def _count_result(self, result: str) -> None:
        """Count what the store said, without treating every answer as stored."""
        self.counts.note(result or "unknown")
        if result == "stored":
            self.counts.stored += 1
        elif result == "duplicate":
            self.counts.duplicate += 1
        elif result == "rejected":
            self.counts.rejected += 1
        elif result in {"conflict", "run_conflict"}:
            self.counts.conflict += 1
        else:
            self.counts.failed += 1


def _signal(writer: MeasurementWriter) -> None:
    """Wake a waiting worker, if the loop is running."""
    loop, wake = writer._loop, writer._wake
    if loop is None or wake is None or loop.is_closed():
        return
    # The loop can close between the check and the call.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(wake.set)


#: The process-wide queue. One destination, so one queue: the design bounds
#: each destination separately, and this process writes to one table.
_writer: MeasurementWriter | None = None
_writer_lock = threading.Lock()


def writer() -> MeasurementWriter:
    """Return the process's write queue, creating it on first use."""
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = MeasurementWriter()
        return _writer


def reset_writer(writer_: MeasurementWriter | None = None) -> None:
    """Replace the process's write queue. Tests call this."""
    global _writer
    with _writer_lock:
        _writer = writer_


__all__ = [
    "MAX_CONCURRENT_WRITES",
    "MAX_QUEUED_BYTES",
    "MAX_QUEUED_MEASUREMENTS",
    "SHUTDOWN_DRAIN_S",
    "WRITE_TIMEOUT_S",
    "MeasurementWriter",
    "WriteCounts",
    "record_size",
    "reset_writer",
    "writer",
]
