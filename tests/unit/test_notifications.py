"""The one connection per process that listens for Postgres notifications.

It serves every receiver on that single connection, hands each channel's
payload to its own receiver, and resets all of them when the connection opens
or is lost: a notification sent while nothing listened is gone, so whoever
waits must read once to find out. The trigger side is in the ``dbtest`` tier.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from cloud_agent.workers import notifications
from cloud_agent.workers.job_event_signal import JobEventSignal
from cloud_agent.workers.notifications import listen
from cloud_agent.workers.work_signal import WorkSignal


class FakeConnection:
    """The part of an asyncpg connection the listener uses."""

    def __init__(self, *, ping_hangs: bool = False) -> None:
        self.ping_hangs = ping_hangs
        self.listeners: dict[str, Any] = {}
        self.on_termination: list[Any] = []
        self.closed = False
        self.terminated = False

    def add_termination_listener(self, callback: Any) -> None:
        self.on_termination.append(callback)

    async def add_listener(self, channel: str, callback: Any) -> None:
        self.listeners[channel] = callback

    async def fetchval(self, _query: str) -> int:
        if self.ping_hangs:
            await asyncio.sleep(60)
        return 1

    def is_closed(self) -> bool:
        return self.closed

    def terminate(self) -> None:
        self.terminated = True
        self.lose()

    def deliver(self, channel: str, payload: str = "") -> None:
        self.listeners[channel](self, 4321, channel, payload)

    def lose(self) -> None:
        self.closed = True
        for callback in self.on_termination:
            callback(self)


async def _eventually(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    pytest.fail("the listener never got there")


class Database:
    """Every connection the listener opens; the first one's ping can hang."""

    def __init__(self) -> None:
        self.opened: list[FakeConnection] = []
        self.first_ping_hangs = False

    async def connect(self, dsn: str) -> FakeConnection:
        assert dsn == "postgresql://test-only"
        connection = FakeConnection(ping_hangs=self.first_ping_hangs and not self.opened)
        self.opened.append(connection)
        return connection


@pytest.fixture
def database(monkeypatch) -> Database:
    database = Database()
    monkeypatch.setattr(asyncpg, "connect", database.connect)
    monkeypatch.setattr(notifications, "_RECONNECT_S", 0.0)
    return database


def _listening(database: Database, index: int) -> bool:
    return len(database.opened) > index and set(database.opened[index].listeners) == {
        WorkSignal.channel,
        JobEventSignal.channel,
    }


async def test_one_connection_serves_every_channel_and_reconnects_after_a_loss(
    database, monkeypatch
):
    warnings: list[str] = []
    monkeypatch.setattr(notifications.logger, "warning", lambda event, **_k: warnings.append(event))
    work = WorkSignal()
    jobs = JobEventSignal()
    listener = asyncio.create_task(listen("postgresql://test-only", (work, jobs)))
    try:
        await _eventually(lambda: _listening(database, 0))
        first = database.opened[0]
        assert jobs.listening

        held = work.token
        parked = asyncio.create_task(work.wait(held, 30))
        await asyncio.sleep(0)
        first.deliver(WorkSignal.channel)
        assert await asyncio.wait_for(parked, 1) != held

        # A job's notification wakes the streams of that job and no other.
        with jobs.subscribe("ajob_1") as one, jobs.subscribe("ajob_2") as two:
            first.deliver(JobEventSignal.channel, "ajob_1")
            assert one.is_set() and not two.is_set()
            one.clear()

            # Whatever was notified while the connection was down is gone, so
            # the loss wakes everyone, and so does the next connection.
            held = work.token
            parked = asyncio.create_task(work.wait(held, 30))
            await asyncio.sleep(0)
            first.lose()
            assert await asyncio.wait_for(parked, 1) != held
            assert one.is_set() and two.is_set()
            await _eventually(lambda: _listening(database, 1))
            assert jobs.listening
        assert warnings == ["agent_notification_listener_failed"]
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
    assert database.opened[1].terminated, "a cancelled listener does not leave its connection open"


async def test_a_connection_that_stops_answering_is_replaced(database, monkeypatch):
    database.first_ping_hangs = True
    monkeypatch.setattr(notifications, "_PING_S", 0.01)
    monkeypatch.setattr(notifications, "_PING_TIMEOUT_S", 0.01)
    listener = asyncio.create_task(
        listen("postgresql://test-only", (WorkSignal(), JobEventSignal()))
    )
    try:
        await _eventually(lambda: len(database.opened) == 2)
        assert database.opened[0].terminated, "terminated, not closed: close would wait on it"
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
