"""One lock per Kata guest for its trusted observer's lifecycle.

The observer is a singleton inside each guest. Starting it, rotating its
journal and fencing a turn's cursor must not interleave on one guest: a
restart invalidates every cursor open there. Nothing about one guest needs to
wait for another's, so ``ChatSandboxManager`` keeps one
:class:`GuestObserverLocks` and enters only the container's own lock to begin
or finish an observation on it.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.guest_observer import GuestObserverCursor


@dataclass
class GuestObserverState:
    """One container's observer lock and the cursors open on its journal."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    #: ``id(cursor)`` to the observer pid that cursor was fenced on.
    active: dict[int, int] = field(default_factory=dict)
    #: Callers holding or waiting for ``lock``; see :class:`GuestObserverLocks`.
    holders: int = 0

    def admit(self, cursor: GuestObserverCursor) -> None:
        """Record a cursor ``begin`` opened; an unavailable one holds nothing."""
        if not cursor.available:
            return
        # A restart invalidates every old-pid cursor already; do not let
        # abandoned bookkeeping suppress future safe rotations.
        self.active = {
            token: observer_pid
            for token, observer_pid in self.active.items()
            if observer_pid == cursor.observer_pid
        }
        self.active[id(cursor)] = cursor.observer_pid

    def release(self, cursor: GuestObserverCursor) -> None:
        """Forget a cursor once its observation has been closed."""
        self.active.pop(id(cursor), None)


class GuestObserverLocks:
    """Serialize each container's observer lifecycle, and only that container's.

    Fork siblings in one container admit turns one at a time, while a begin on
    another container -- two execs, or a whole restart -- never waits behind
    them. A container's entry lives while a caller holds or waits for its
    lock, or a cursor is open on it, so the map does not grow with every
    container the manager has ever observed.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._containers: dict[str, GuestObserverState] = {}

    @contextlib.contextmanager
    def container(self, key: str) -> Iterator[GuestObserverState]:
        """Hold one container's observer lock and its cursor bookkeeping."""
        with self._guard:
            state = self._containers.get(key)
            if state is None:
                state = self._containers[key] = GuestObserverState()
            state.holders += 1
        try:
            with state.lock:
                yield state
        finally:
            with self._guard:
                state.holders -= 1
                if not state.holders and not state.active:
                    self._containers.pop(key, None)


__all__ = ["GuestObserverLocks", "GuestObserverState"]
