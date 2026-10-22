"""The ordered sender behind a runner's event uploads.

The turn loop reports each normalized event as it reads it. It used to post the
event before reading the next one, so a slow or restarting control plane held up
the whole stream, and a 300-tool turn cost over a thousand round trips. Events
now wait here, encoded, for one thread that delivers them in the order they were
queued, as many to an upload as fit. This module owns that machinery: the
bounded queue, the batching, the drain, and how a failure reaches the caller.
What an upload is, and how it is retried, belongs to
:mod:`cloud_agent_host.control_plane_client`.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING

from cloud_agent_host.claimed_job import LeaseLost
from cloud_agent_shared.event_payloads import MAX_EVENT_BATCH_EVENTS

if TYPE_CHECKING:
    from collections.abc import Callable

#: How the sender coalesces events: at most this many bytes of JSON in one
#: upload (an event larger than that goes alone), besides the shared count. An
#: event that finds the sender idle waits this long for company; the ones that
#: arrive while an upload is in flight have waited a round trip already, and go
#: as soon as it returns. A slower control plane therefore gets fewer, larger
#: uploads instead of a slower turn.
_EVENT_BATCH_MAX_BYTES = 256 * 1024
_EVENT_BATCH_LINGER_S = 0.05
#: How much the sender holds before a new event waits for room. That wait is the
#: backpressure that stops a CLI whose events cannot be recorded: its output is
#: no longer read, as when each event was posted before the next was read.
_EVENT_QUEUE_MAX_EVENTS = 1000
_EVENT_QUEUE_MAX_BYTES = 4 * 1024 * 1024


class OrderedSender:
    """Queue encoded events and deliver them, oldest first, from one thread.

    ``deliver`` stores one batch in order or raises why it could not. It is
    also given a callable that says whether the sender was closed meanwhile,
    so that a retry can stop early. A batch it gives up on is kept for the
    next :meth:`put` or :meth:`flush` to raise, and the events queued behind
    it are dropped with it: the turn that queued them is ending.
    """

    def __init__(self, deliver: Callable[[list[bytes], Callable[[], bool]], None]) -> None:
        self._deliver = deliver
        # Guards every field below, down to `lease_lost`.
        self._state = threading.Condition()
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self._in_flight = 0
        self._thread: threading.Thread | None = None
        # Closing starts a new epoch, which a thread of an older one stops at.
        self._epoch = 0
        self._flushing = 0
        self._failure: Exception | None = None
        #: Set once an upload was refused for a lost lease.
        self.lease_lost = False

    def put(self, body: bytes) -> None:
        """Queue one encoded event, waiting while the sender holds its fill.

        Raises what made the sender give up, before queueing anything.
        """
        with self._state:
            self._raise_failure()
            while self._pending and (
                len(self._pending) >= _EVENT_QUEUE_MAX_EVENTS
                or self._pending_bytes + len(body) > _EVENT_QUEUE_MAX_BYTES
            ):
                self._state.wait()
                self._raise_failure()
            self._pending.append(body)
            self._pending_bytes += len(body)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, args=(self._epoch,), daemon=True, name="agent-event-sender"
                )
                self._thread.start()
            self._state.notify_all()

    def flush(self) -> None:
        """Wait until every queued event is delivered, or raise what stopped one.

        The sender stops waiting for company meanwhile.
        """
        with self._state:
            epoch = self._epoch
            self._flushing += 1
            self._state.notify_all()
            try:
                self._state.wait_for(
                    lambda: (
                        (not self._pending and not self._in_flight)
                        or self._failure is not None
                        or self._epoch != epoch
                    )
                )
            finally:
                self._flushing -= 1
            self._raise_failure()

    def close(self) -> None:
        """Stop the sending thread at its next look, and drop what is still queued."""
        with self._state:
            self._epoch += 1
            self._thread = None
            self._pending.clear()
            self._pending_bytes = 0
            self._in_flight = 0
            self._state.notify_all()

    def _raise_failure(self) -> None:
        """Raise what made the sender give up: once, or every time for a lost lease.

        Once, because the caller it reaches ends the turn, and that turn still
        reports its error and finishes through the same sender.
        """
        failure = self._failure
        if isinstance(failure, LeaseLost):
            raise LeaseLost(str(failure)) from failure
        if failure is not None:
            self._failure = None
            raise failure

    def _run(self, epoch: int) -> None:
        """Deliver queued events, oldest first and coalesced, until closed."""
        idle = True
        while True:
            with self._state:
                self._state.wait_for(lambda: self._pending or self._epoch != epoch)
                if idle:
                    self._state.wait_for(
                        lambda: (
                            self._epoch != epoch
                            or self._flushing > 0
                            or len(self._pending) >= MAX_EVENT_BATCH_EVENTS
                            or self._pending_bytes >= _EVENT_BATCH_MAX_BYTES
                        ),
                        timeout=_EVENT_BATCH_LINGER_S,
                    )
                if self._epoch != epoch:
                    return
                batch = self._take_batch()
            failure: Exception | None = None
            try:
                self._deliver(batch, lambda: self._epoch != epoch)
            except Exception as exc:
                failure = exc
            with self._state:
                if self._epoch != epoch:
                    return
                self._in_flight = 0
                if failure is not None:
                    self._failure = failure
                    if isinstance(failure, LeaseLost):
                        self.lease_lost = True
                    self._pending.clear()
                    self._pending_bytes = 0
                idle = not self._pending
                self._state.notify_all()

    def _take_batch(self) -> list[bytes]:
        """Take the oldest events that fit in one upload; the first always does."""
        batch = [self._pending.popleft()]
        size = len(batch[0])
        while (
            self._pending
            and len(batch) < MAX_EVENT_BATCH_EVENTS
            and size + len(self._pending[0]) <= _EVENT_BATCH_MAX_BYTES
        ):
            size += len(self._pending[0])
            batch.append(self._pending.popleft())
        self._pending_bytes -= size
        self._in_flight = len(batch)
        self._state.notify_all()
        return batch
