"""Collect, per owner, which of their jobs and threads changed, and wake their tabs.

Triggers notify ``agent_owner_changes`` with the owner, the kind of row and its
id (migrations ``0031`` and ``0032``), and the process's shared listener
(``notifications``) hands it here. Each open stream of that owner adds the id
to its own pending set and is woken; it then reads changed jobs and threads and
sends their current values, and forwards the other kinds as hints.

A stream subscribes before it says hello, so anything that changes after the
tab's own snapshot is in its pending set. When the listening connection opens
or is lost, whatever was notified meanwhile is unknown, so every stream is told
to resynchronise: its tab reads its lists again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable, Iterator

logger = get_logger(__name__)

_T = TypeVar("_T")

CHANNEL = "agent_owner_changes"
#: Kinds the stream forwards as a hint, without reading anything: the tab reads
#: that one thing again. ``usage`` and ``mcp`` name a job; ``queue`` and
#: ``publication`` name a conversation (migration ``0032``).
HINT_KINDS = ("usage", "queue", "mcp", "publication")


@dataclass
class OwnerChanges:
    """What changed for one owner since a stream last looked."""

    jobs: set[str] = field(default_factory=set)
    threads: set[str] = field(default_factory=set)
    hints: dict[str, set[str]] = field(default_factory=dict)
    resync: bool = False
    #: The newest notification among these changes, by the signal's count: a
    #: read made for them must have started at or after it.
    sequence: int = 0


class OwnerSubscription:
    """One stream's pending changes, and the wake-up that says there are some."""

    def __init__(self) -> None:
        """Start with nothing pending."""
        self.woken = asyncio.Event()
        self._pending = OwnerChanges()

    def add(self, kind: str, row_id: str, sequence: int = 0) -> None:
        """Record one changed row, notified as the signal's ``sequence``-th."""
        if kind == "job":
            self._pending.jobs.add(row_id)
        elif kind == "thread":
            self._pending.threads.add(row_id)
        elif kind in HINT_KINDS:
            self._pending.hints.setdefault(kind, set()).add(row_id)
        else:
            return
        self._pending.sequence = max(self._pending.sequence, sequence)
        self.woken.set()

    def resync(self) -> None:
        """Record that changes may have been missed."""
        self._pending.resync = True
        self.woken.set()

    def take(self) -> OwnerChanges:
        """Hand over everything pending and start collecting afresh."""
        taken, self._pending = self._pending, OwnerChanges()
        return taken

    async def wait(self, timeout_s: float) -> bool:
        """Wait for a change or the timeout; report which, and consume the wake-up."""
        try:
            await asyncio.wait_for(self.woken.wait(), timeout_s)
        except TimeoutError:
            return False
        finally:
            self.woken.clear()
        return True


class SharedReads:
    """One read per key at a time, shared by the streams that need it.

    Every tab an owner has open is woken by the same notification, and each
    would otherwise make the same read. A stream joins a read already under way
    only if that read started at or after the newest notification the stream
    is answering: one that started earlier may not include what it was told
    changed. The read runs as its own task, so a stream that goes away does not
    cancel it for the others.
    """

    def __init__(self, clock: Callable[[], int]) -> None:
        """Date each read by ``clock``, the count of notifications delivered."""
        self._clock = clock
        self._running: dict[Hashable, tuple[int, asyncio.Task[Any]]] = {}

    async def read(self, key: Hashable, *, after: int, read: Callable[[], Awaitable[_T]]) -> _T:
        """Return ``read()``, or the same read another stream started since ``after``."""
        running = self._running.get(key)
        if running is None or running[0] < after:
            started = self._clock()
            task: asyncio.Task[Any] = asyncio.ensure_future(read())
            running = (started, task)
            self._running[key] = running
            task.add_done_callback(partial(self._finished, key))
        result: _T = await asyncio.shield(running[1])
        return result

    def _finished(self, key: Hashable, task: asyncio.Task[Any]) -> None:
        running = self._running.get(key)
        if running is not None and running[1] is task:
            del self._running[key]
        if not task.cancelled():
            # Every stream that wanted it may be gone; the error is not lost
            # on them, so do not report it as never retrieved.
            task.exception()

    def running(self) -> int:
        """How many reads are under way; for tests and diagnostics."""
        return len(self._running)


class OwnerChangeSignal:
    """Per-owner change sets for the change streams of this process."""

    channel = CHANNEL

    def __init__(self) -> None:
        """Start with nobody subscribed and no connection listening yet."""
        self._waiting: dict[str, set[OwnerSubscription]] = {}
        #: Whether a connection is listening now. While it is not, nothing can
        #: be pushed, and tabs keep reading on their own clocks.
        self.listening = False
        #: How many notifications this process has been handed.
        self.sequence = 0
        #: Reads the owner's streams share; see :class:`SharedReads`.
        self.reads = SharedReads(lambda: self.sequence)

    @contextlib.contextmanager
    def subscribe(self, user_id: str) -> Iterator[OwnerSubscription]:
        """Collect ``user_id``'s changes for as long as the block runs."""
        subscription = OwnerSubscription()
        self._waiting.setdefault(user_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            waiting = self._waiting.get(user_id)
            if waiting is not None:
                waiting.discard(subscription)
                if not waiting:
                    del self._waiting[user_id]

    def notified(self, payload: str) -> None:
        """One row of one owner changed; the payload is the trigger's JSON."""
        try:
            change = json.loads(payload)
            user_id, kind, row_id = str(change["user"]), str(change["kind"]), str(change["id"])
        except (ValueError, TypeError, KeyError):
            logger.warning(
                "agent_owner_change_unreadable", extra={"event": "agent_owner_change_unreadable"}
            )
            return
        self.sequence += 1
        for subscription in self._waiting.get(user_id, ()):
            subscription.add(kind, row_id, self.sequence)

    def reset(self, *, listening: bool) -> None:
        """The connection opened or was lost: every stream resynchronises."""
        self.listening = listening
        for waiting in self._waiting.values():
            for subscription in waiting:
                subscription.resync()

    def subscribers(self, user_id: str) -> int:
        """How many streams collect ``user_id``'s changes; for tests and diagnostics."""
        return len(self._waiting.get(user_id, ()))
