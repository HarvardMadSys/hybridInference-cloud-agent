"""One connection per process that listens for Postgres notifications.

Triggers notify when something a request is waiting on may have changed: a job
became claimable (``agent_work_available``, migration ``0029``), a job stored
an event or changed state (``agent_job_events``, migration ``0030``), and so on.
Each channel has a receiver in this process that wakes whoever waits on it.

A notification is only a hint to read again; the rows stay the record. One sent
while nothing listened is gone, so every connect and every loss resets each
receiver, which wakes all of its waiters to read once and find out.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    import asyncpg

logger = get_logger(__name__)

#: How often the idle listening connection is checked. One that died without
#: closing would otherwise leave every wait to run out its timeout unnoticed.
_PING_S = 30.0
_PING_TIMEOUT_S = 10.0
_RECONNECT_S = 5.0


class Receiver(Protocol):
    """What the listener needs from each channel's signal."""

    channel: str

    def notified(self, payload: str) -> None:
        """Wake whoever waits on what ``payload`` names."""

    def reset(self, *, listening: bool) -> None:
        """The connection opened or was lost: wake every waiter to read once."""


class KeyedSignal:
    """Wake-ups by key, for a channel whose notifications name one.

    A waiter subscribes *before* it reads and consumes its wake-up *after* it
    waits, so a notification that arrives while it is reading is still waiting
    for it: nothing stored between a read and the next wait is left for the
    timeout.
    """

    channel: str

    def __init__(self) -> None:
        """Start with nobody waiting and no connection listening yet."""
        self._waiting: dict[str, set[asyncio.Event]] = {}
        #: Whether a connection is listening now. Until it is, and after it is
        #: lost, nothing would wake a waiter, so waiters look on a short clock.
        self.listening = False

    @contextlib.contextmanager
    def subscribe(self, key: str) -> Iterator[asyncio.Event]:
        """Register a wake-up for ``key`` for as long as the block runs."""
        woken = asyncio.Event()
        self._waiting.setdefault(key, set()).add(woken)
        try:
            yield woken
        finally:
            waiting = self._waiting.get(key)
            if waiting is not None:
                waiting.discard(woken)
                if not waiting:
                    del self._waiting[key]

    @staticmethod
    async def wait(woken: asyncio.Event, timeout_s: float) -> bool:
        """Wait for a wake-up or the timeout; report which, and consume it."""
        try:
            # A zero timeout must still see a wake-up that already came.
            if not woken.is_set():
                await asyncio.wait_for(woken.wait(), timeout_s)
        except TimeoutError:
            return False
        finally:
            woken.clear()
        return True

    def notified(self, payload: str) -> None:
        """Something changed for the key ``payload`` names."""
        for woken in self._waiting.get(payload, ()):
            woken.set()

    def reset(self, *, listening: bool) -> None:
        """The connection opened or was lost: every waiter reads once to catch up."""
        self.listening = listening
        for waiting in self._waiting.values():
            for woken in waiting:
                woken.set()

    def subscribers(self, key: str) -> int:
        """How many wait on ``key``; for tests and diagnostics."""
        return len(self._waiting.get(key, ()))


async def listen(dsn: str, receivers: Sequence[Receiver]) -> None:
    """Keep one connection listening on every receiver's channel until cancelled.

    A dedicated connection, because a pooled one is reset when released and
    would stop listening.
    """
    import asyncpg

    healthy = True
    while True:
        connection: asyncpg.Connection | None = None
        try:
            connection = await asyncpg.connect(dsn)
            lost = asyncio.Event()
            # Bound now: a late callback from an old connection must not end
            # the next one.
            connection.add_termination_listener(lambda _connection, lost=lost: lost.set())
            for receiver in receivers:
                await connection.add_listener(receiver.channel, _deliver_to(receiver))
            for receiver in receivers:
                receiver.reset(listening=True)
            if not healthy:
                healthy = True
                logger.info(
                    "agent_notification_listener_recovered",
                    extra={"event": "agent_notification_listener_recovered"},
                )
            while not lost.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(lost.wait(), _PING_S)
                if not lost.is_set():
                    await asyncio.wait_for(connection.fetchval("SELECT 1"), _PING_TIMEOUT_S)
            raise ConnectionError("the listening connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Once per outage: waiters read on their own timeouts meanwhile,
            # so a report every reconnect would say nothing new.
            if healthy:
                healthy = False
                logger.warning(
                    "agent_notification_listener_failed",
                    extra={
                        "event": "agent_notification_listener_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    },
                )
        finally:
            if connection is not None and not connection.is_closed():
                # Not close(): that waits on the server, and the server may be
                # what stopped answering.
                connection.terminate()
        for receiver in receivers:
            receiver.reset(listening=False)
        await asyncio.sleep(_RECONNECT_S)


def _deliver_to(receiver: Receiver) -> Any:
    """The asyncpg callback that hands one channel's payload to its receiver."""

    def deliver(_connection: Any, _pid: int, _channel: str, payload: str) -> None:
        receiver.notified(payload)

    return deliver
