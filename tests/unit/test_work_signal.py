"""The signal idle hosts wait on, and the connection that drives it.

What has to hold is that a host never waits out an interval for work it could
have been told about: a change between its claim and its next wait answers that
wait at once, and so does a token from before a restart or a lost connection.
The trigger side is in the ``dbtest`` tier, against a real database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from cloud_agent.workers import work_signal
from cloud_agent.workers.work_signal import CHANNEL, WorkSignal, listen_for_work


async def test_a_wait_without_the_current_token_is_answered_at_once():
    signal = WorkSignal()
    assert await asyncio.wait_for(signal.wait(None, 30), 1) == signal.token
    # From an earlier process: what changed since is unknown.
    assert await asyncio.wait_for(signal.wait(WorkSignal().token, 30), 1) == signal.token


async def test_work_queued_while_the_host_claimed_answers_its_next_wait():
    signal = WorkSignal()
    held_before_claim = signal.token
    signal.notify()
    assert await asyncio.wait_for(signal.wait(held_before_claim, 30), 1) == signal.token
    assert signal.token != held_before_claim


async def test_every_parked_wait_is_released_by_the_next_change():
    signal = WorkSignal()
    current = signal.token
    parked = [asyncio.create_task(signal.wait(current, 30)) for _ in range(3)]
    await asyncio.sleep(0)
    assert not any(task.done() for task in parked)

    signal.notify()

    assert await asyncio.wait_for(asyncio.gather(*parked), 1) == [signal.token] * 3


async def test_with_no_change_a_wait_ends_at_its_timeout_with_the_same_token():
    signal = WorkSignal()
    current = signal.token
    assert await signal.wait(current, 0.01) == current


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

    def deliver(self) -> None:
        self.listeners[CHANNEL](self, 4321, CHANNEL, "")

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
    monkeypatch.setattr(work_signal, "_RECONNECT_S", 0.0)
    return database


async def test_the_listener_wakes_waiters_and_reconnects_after_a_loss(database, monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(work_signal.logger, "warning", lambda event, **_k: warnings.append(event))
    signal = WorkSignal()
    listener = asyncio.create_task(listen_for_work("postgresql://test-only", signal))
    try:
        await _eventually(lambda: database.opened and CHANNEL in database.opened[0].listeners)
        first = database.opened[0]

        held = signal.token
        parked = asyncio.create_task(signal.wait(held, 30))
        await asyncio.sleep(0)
        first.deliver()
        assert await asyncio.wait_for(parked, 1) != held

        # Whatever was notified while the connection was down is gone, so
        # the loss wakes everyone, and so does the next connection.
        held = signal.token
        parked = asyncio.create_task(signal.wait(held, 30))
        await asyncio.sleep(0)
        first.lose()
        assert await asyncio.wait_for(parked, 1) != held
        await _eventually(
            lambda: len(database.opened) == 2 and CHANNEL in database.opened[1].listeners
        )
        assert warnings == ["agent_work_listener_failed"]
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
    assert database.opened[1].terminated, "a cancelled listener does not leave its connection open"


async def test_a_connection_that_stops_answering_is_replaced(database, monkeypatch):
    database.first_ping_hangs = True
    monkeypatch.setattr(work_signal, "_PING_S", 0.01)
    monkeypatch.setattr(work_signal, "_PING_TIMEOUT_S", 0.01)
    signal = WorkSignal()
    listener = asyncio.create_task(listen_for_work("postgresql://test-only", signal))
    try:
        await _eventually(lambda: len(database.opened) == 2)
        assert database.opened[0].terminated, "terminated, not closed: close would wait on it"
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
