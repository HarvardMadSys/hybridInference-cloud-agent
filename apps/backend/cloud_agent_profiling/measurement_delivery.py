"""Delivery of finished measurements: what to retry, and what to give up on.

A measurement is diagnostic. Nothing about it may slow down, fail or block the
request it describes, so every decision here is written to keep the cost on the
*dropping* side rather than the waiting side.

Two questions are kept apart on purpose:

* **Is this attempt's outcome worth another attempt?** Network failures and a
  server that is briefly unavailable are; a rejected schema, a bad token and an
  idempotency conflict are not. Retrying the second kind cannot succeed --
  sending the same bytes to a service that refused them for what they are --
  and looping would turn a disagreement into an outage of its own.
* **How much may pile up before we drop?** Both a count and a total payload
  size, because a queue bounded only by count can still hold hundreds of
  megabytes, and one bounded only by bytes can still hold an unbounded number
  of tiny records.

Dropping is always reported by count. A measurement lost to a full queue is
indistinguishable, later, from one that was never taken -- so the count is the
only thing that keeps the gap honest, and it is never silently a zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: How many finished measurements may wait for delivery in one producer. The
#: same bound the local exporter uses: reaching it means delivery cannot keep
#: up with collection, which is reported rather than hidden.
DELIVERY_QUEUE_MAX: Final = 256

#: Total payload that may wait, in bytes. Count alone is not a bound on memory.
DELIVERY_BYTES_MAX: Final = 4 * 1024 * 1024

#: Attempts one record gets, including the first. A record that cannot be
#: delivered in three tries is not going to be delivered by a fourth.
DELIVERY_ATTEMPTS_MAX: Final = 3

#: Connect and read budget for one attempt.
DELIVERY_TIMEOUT_S: Final = 10.0

#: How long stopping may spend flushing, so a shutdown is not held hostage by
#: a destination that is not answering.
DELIVERY_DRAIN_S: Final = 2.0

#: A prompt handoff starts delivery but never waits for it.  Waiting here is
#: hidden user latency after the startup boundary and before model output is
#: read; the turn's exit performs the bounded drain.
DELIVERY_HANDOFF_DRAIN_S: Final = 0.0

#: Statuses worth trying again: the destination asked for time, or a proxy in
#: front of it did. Everything else is a statement about the request.
RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504, 507})


@dataclass
class DeliveryOutcome:
    """What happened to one record, and what should happen next."""

    #: ``"delivered"``, ``"duplicate"``, ``"retry"`` or ``"dropped"``.
    result: str
    reason: str = ""
    #: Populated when the attempt raised rather than returning a status.
    error: BaseException | None = None

    @property
    def delivered(self) -> bool:
        """Return whether the destination has this record and is done with it."""
        return self.result in {"delivered", "duplicate"}

    @property
    def retryable(self) -> bool:
        """Return whether another attempt could plausibly succeed."""
        return self.result == "retry"


def classify_response(status_code: int, body: Mapping[str, Any] | None = None) -> DeliveryOutcome:
    """Return what one HTTP response means for delivery.

    Only ``stored`` and ``duplicate`` are success. A 2xx is the transport
    saying the request was understood; the body says what was done with it, and
    ``conflict``, ``run_conflict`` and ``rejected`` are refusals that arrive
    with a perfectly good status code. A refusal the service will repeat is
    terminal on the first answer, because a second identical request cannot
    change it.
    """
    if status_code in {200, 201, 202, 204}:
        stored = str((body or {}).get("result") or "stored")
        if stored in {"conflict", "run_conflict", "rejected"}:
            # The destination refused the record and will refuse it again. A
            # 200 says the request was understood, not that the measurement was
            # kept, and reporting a refusal as delivered is how a stored table
            # silently acquires a gap nobody counted.
            return DeliveryOutcome("dropped", stored)
        if stored == "duplicate":
            return DeliveryOutcome("duplicate")
        if stored != "stored":
            # An outcome this producer does not know cannot be assumed to mean
            # the record is safe.
            return DeliveryOutcome("dropped", f"unknown result {stored!r}")
        return DeliveryOutcome("delivered")
    if status_code == 409:
        return DeliveryOutcome("dropped", "conflict")
    if status_code in RETRYABLE_STATUS:
        return DeliveryOutcome("retry", f"status {status_code}")
    # 400, 401, 403, 404, 413, 422 and anything else unexpected: the service
    # has judged this request, and sending it again sends the same judgement.
    return DeliveryOutcome("dropped", f"status {status_code}")


def classify_exception(exc: BaseException) -> DeliveryOutcome:
    """Return what a raised attempt means for delivery.

    Transport failures are retried because they say nothing about the request.
    Anything else -- a bug in the serialization, a cancelled task -- is not
    something a retry fixes, so it is dropped with its error attached.
    """
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return DeliveryOutcome("retry", "transport", error=exc)
    return DeliveryOutcome("dropped", "unexpected", error=exc)


def attempts_exhausted(attempts: int) -> bool:
    """Return whether a record has used its attempts and must be given up on."""
    return attempts >= DELIVERY_ATTEMPTS_MAX


@dataclass
class DeliveryQueue:
    """A bounded queue of finished measurements waiting to be sent.

    Not a transport: it holds records and accounts for what it had to drop, so
    the caller that owns the destination can drain it. Both bounds are enforced
    on offer, and neither is allowed to block the producer -- a diagnostic that
    can stall the thing it measures is worse than no diagnostic.
    """

    max_items: int = DELIVERY_QUEUE_MAX
    max_bytes: int = DELIVERY_BYTES_MAX
    _items: list[tuple[str, bytes]] = field(default_factory=list)
    _bytes: int = 0
    dropped_items: int = 0
    dropped_bytes: int = 0

    def offer(self, line: bytes) -> bool:
        """Queue one already-serialized record. Returns whether it was kept."""
        if len(line) > self.max_bytes:
            # A single record larger than the whole budget would evict
            # everything else and still not fit, so it is refused outright.
            self.dropped_items += 1
            self.dropped_bytes += len(line)
            return False
        if len(self._items) >= self.max_items or self._bytes + len(line) > self.max_bytes:
            self.dropped_items += 1
            self.dropped_bytes += len(line)
            return False
        self._items.append(("", line))
        self._bytes += len(line)
        return True

    def take(self) -> tuple[str, bytes] | None:
        """Remove and return the oldest record, or ``None`` when empty."""
        if not self._items:
            return None
        _, line = self._items.pop(0)
        self._bytes -= len(line)
        return ("", line)

    def __len__(self) -> int:
        """Return how many records are waiting."""
        return len(self._items)

    @property
    def pending_bytes(self) -> int:
        """Return how many payload bytes are waiting."""
        return self._bytes

    def dropped(self) -> dict[str, int]:
        """Return the drop counters, for the experiment's diagnostics."""
        return {"items": self.dropped_items, "bytes": self.dropped_bytes}


__all__ = [
    "DELIVERY_ATTEMPTS_MAX",
    "DELIVERY_BYTES_MAX",
    "DELIVERY_DRAIN_S",
    "DELIVERY_HANDOFF_DRAIN_S",
    "DELIVERY_QUEUE_MAX",
    "DELIVERY_TIMEOUT_S",
    "RETRYABLE_STATUS",
    "DeliveryOutcome",
    "DeliveryQueue",
    "attempts_exhausted",
    "classify_exception",
    "classify_response",
]
