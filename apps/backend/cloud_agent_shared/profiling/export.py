"""Publish frozen records through a bounded background queue."""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import threading
from typing import Any

from .clock import clock
from .schema import ENV_PROFILING_SINK, SINK_QUEUE_MAX

logger = logging.getLogger("cloud_agent_shared.operation_profiling")


class _Exporter:
    """Publish records from a bounded background queue.

    Both destinations move off the attempt thread, not just the file. The
    record is produced while the sandbox is starting, so anything synchronous
    here -- a JSONL write, or a log handler that blocks on a slow socket, a
    full disk or a synchronous shipper -- is charged to the startup latency the
    profile exists to explain. Logging is not exempt just because it is the
    established path.

    One daemon thread drains the queue, and it starts only once a live run has
    a record to export. The queue is bounded: an exporter that cannot keep up
    drops records and counts them rather than growing without limit or
    blocking the caller.
    """

    def __init__(self, *, maxsize: int = SINK_QUEUE_MAX) -> None:
        self.dropped = 0
        #: A process that cannot start the thread can still serve traffic. It
        #: keeps running with the measurement dropped rather than failing the
        #: startup that asked for it.
        self.disabled = False
        self._queue: queue.Queue[tuple[str, str | None] | None] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._pending = 0
        self._closing = False
        self._warned = False
        self._drops_reported = False
        self._thread = threading.Thread(
            target=self._run, name="operation-profile-sink", daemon=True
        )
        try:
            self._thread.start()
        except Exception as exc:
            # A thread limit, a fork in a bad state, a sandbox refusing
            # clone(): none of that is a reason to fail the business call this
            # measurement exists to observe, and this runs while a prompt has
            # already been sent.
            self.disabled = True
            self._closing = True
            logger.warning(
                "operation profiling could not start its exporter (%s); "
                "measurements will be dropped",
                exc,
            )
            return
        atexit.register(self._flush_before_exit)

    def offer(self, line: str, sink: str | None) -> None:
        """Queue one record. Never blocks and never raises."""
        with self._lock:
            if self._closing:
                if self.disabled:
                    self.dropped += 1
                return
            # Counted before the item is visible, so the drain thread can never
            # observe a decrement it did not earn.
            self._pending += 1
        try:
            self._queue.put_nowait((line, sink))
        except queue.Full:
            with self._lock:
                self._pending -= 1
                self.dropped += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                line, sink = item
                self._publish(line, sink)
            finally:
                with self._lock:
                    self._pending -= 1

    def _publish(self, line: str, sink: str | None) -> None:
        """Log one record and, when configured, append it to the sink."""
        logger.info("agent_operation_profile %s", line)
        if not sink:
            return
        try:
            with open(sink, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # One warning per exporter: a broken path would otherwise log on
            # every record for the length of the experiment.
            with self._lock:
                first = not self._warned
                self._warned = True
            if first:
                logger.warning("operation profiling sink %s is unavailable (%s)", sink, exc)

    def flush(self, timeout: float = 2.0) -> None:
        """Wait until queued records are published, or the timeout expires."""
        if self.disabled:
            return
        deadline = clock().monotonic() + timeout
        while clock().monotonic() < deadline:
            with self._lock:
                if self._pending <= 0:
                    return
            clock().sleep(0.005)

    def close(self, timeout: float = 2.0) -> None:
        """Drain, stop the exporter thread, and report anything it dropped."""
        with self._lock:
            self._closing = True
        self.flush(timeout)
        if self.disabled:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)
        with self._lock:
            report, self._drops_reported = self.dropped > 0 and not self._drops_reported, True
            dropped = self.dropped
        if report:
            logger.warning("operation profiling exporter dropped %d record(s)", dropped)

    def _flush_before_exit(self) -> None:
        """Best-effort drain at interpreter shutdown, bounded by the timeout."""
        self.close(timeout=2.0)


_EXPORT_LOCK = threading.Lock()


_EXPORTER: _Exporter | None = None


def _exporter() -> _Exporter:
    """Return the process exporter, starting it on the first record.

    A process that cannot start the thread gets one permanently disabled
    exporter rather than an exception per record: retrying a thread start on
    every measurement would be its own cost, and the point is that the business
    path never learns this failed.
    """
    global _EXPORTER
    with _EXPORT_LOCK:
        if _EXPORTER is None:
            _EXPORTER = _Exporter()
        return _EXPORTER


def flush_sink(timeout: float = 2.0) -> None:
    """Wait for queued records to be logged and written."""
    with _EXPORT_LOCK:
        exporter = _EXPORTER
    if exporter is not None:
        exporter.flush(timeout)


def close_sink(timeout: float = 2.0) -> None:
    """Flush and stop the export thread, if one was ever started."""
    global _EXPORTER
    with _EXPORT_LOCK:
        exporter, _EXPORTER = _EXPORTER, None
    if exporter is not None:
        exporter.close(timeout)


def _emit(record: dict[str, Any]) -> None:
    """Hand one finished record to the export thread.

    Nothing here touches a destination: logging blocks just as a file write
    does, and both would otherwise land inside the startup being measured.
    """
    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - facts are bounded already
        logger.warning("operation profiling could not serialize a measurement")
        return
    sink = (os.environ.get(ENV_PROFILING_SINK) or "").strip() or None
    # Never on the attempt thread: see _Exporter. A measurement that cannot be
    # exported is dropped; it is never allowed to fail the work it observed.
    try:
        _exporter().offer(line, sink)
    except Exception as exc:  # pragma: no cover - guarded in _Exporter too
        logger.warning("operation profiling could not publish a measurement (%s)", exc)
