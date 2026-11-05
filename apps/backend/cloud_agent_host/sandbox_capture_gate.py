"""The host-wide capture slots every archive, checkpoint and restore shares.

:mod:`cloud_agent_host.sandbox_archives` re-exports :class:`CaptureGate`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class CaptureGate:
    """The host's capture slots, handed first to work a person is waiting on.

    A bounded semaphore otherwise: it caps how many captures spool at once,
    and a wait for it ends at its timeout. But a first prompt or a cold start
    must not queue behind a stopped chat's final archive or a checkpoint, so a
    waiting ``urgent`` capture goes before any other; within each kind the
    earliest goes first. A capture overtaken ``max_overtaken`` times goes next
    regardless, so a stream of new chats cannot starve a stop's final archive.

    Going first still waited for a capture already spooling, up to its whole
    deadline. With a ``spool_budget_bytes``, one urgent capture may also spool
    beside the ``capacity`` others, whenever the worst cases of all of them
    (``spool_bytes`` each) fit that budget, so the budget, not the count,
    bounds the scratch disk. Background work never takes that place, and an
    urgent capture taking it overtakes nobody waiting for the others.
    """

    def __init__(
        self, capacity: int = 1, *, max_overtaken: int = 2, spool_budget_bytes: int = 0
    ) -> None:
        self._capacity = capacity
        self._free = capacity
        self._max_overtaken = max_overtaken
        self._spool_budget_bytes = spool_budget_bytes
        self._spooling: list[CaptureWaiter] = []
        self._waiting: list[CaptureWaiter] = []
        self._changed = threading.Condition()

    def acquire(
        self,
        blocking: bool = True,
        timeout: float | None = None,
        *,
        urgent: bool = False,
        spool_bytes: int = 0,
    ) -> CaptureWaiter | None:
        """Take a slot once this capture is next, or return ``None`` at the timeout.

        The slot is what :meth:`release` takes back. ``spool_bytes`` is the
        most this capture can spool at once.
        """
        waiter = CaptureWaiter(urgent=urgent, spool_bytes=spool_bytes)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            self._waiting.append(waiter)
            try:
                while not self._admit(waiter):
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if not blocking or (remaining is not None and remaining <= 0):
                        return None
                    self._changed.wait(remaining)
                self._spooling.append(waiter)
                return waiter
            finally:
                self._waiting.remove(waiter)
                self._changed.notify_all()

    def release(self, slot: CaptureWaiter | None = None) -> None:
        """Return ``slot``, or any shared one, as ``BoundedSemaphore`` does: never more than taken."""
        with self._changed:
            if slot is None:
                slot = next((held for held in self._spooling if not held.beside), None)
            if slot is None or slot not in self._spooling:
                raise ValueError("capture gate released too many times")
            self._spooling.remove(slot)
            if not slot.beside:
                self._free += 1
            self._changed.notify_all()

    def _admit(self, waiter: CaptureWaiter) -> bool:
        if self._free and self._next() is waiter:
            self._free -= 1
            for earlier in self._waiting[: self._waiting.index(waiter)]:
                earlier.overtaken += 1
            return True
        if (
            self._spool_budget_bytes > 0
            and waiter.urgent
            and waiter is next(each for each in self._waiting if each.urgent)
            and not any(held.beside for held in self._spooling)
            and sum(held.spool_bytes for held in self._spooling) + waiter.spool_bytes
            <= self._spool_budget_bytes
        ):
            waiter.beside = True
            return True
        return False

    def _next(self) -> CaptureWaiter:
        for waiter in self._waiting:
            if waiter.overtaken >= self._max_overtaken:
                return waiter
        return next((waiter for waiter in self._waiting if waiter.urgent), self._waiting[0])


@dataclass(eq=False)
class CaptureWaiter:
    """One capture waiting for a slot, then holding it. Identity tells two apart."""

    urgent: bool
    spool_bytes: int = 0
    overtaken: int = 0
    #: Holds the urgent place beside the shared slots, not one of them.
    beside: bool = False


__all__ = ["CaptureGate", "CaptureWaiter"]
