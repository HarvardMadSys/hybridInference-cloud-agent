"""The signal idle hosts wait on.

What has to hold is that a host never waits out an interval for work it could
have been told about: a change between its claim and its next wait answers that
wait at once, and so does a token from before a restart or a lost connection.
The connection that drives it is ``test_notifications.py``; the trigger side is
in the ``dbtest`` tier, against a real database.
"""

from __future__ import annotations

import asyncio

from cloud_agent.workers.work_signal import WorkSignal


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


async def test_a_notification_or_a_reset_of_the_listener_releases_parked_waits():
    """Either way a host claims to find out; missing work costs it a whole wait."""
    signal = WorkSignal()
    for wake in (
        lambda: signal.notified(""),
        lambda: signal.reset(listening=True),
        lambda: signal.reset(listening=False),
    ):
        current = signal.token
        parked = asyncio.create_task(signal.wait(current, 30))
        await asyncio.sleep(0)
        wake()
        assert await asyncio.wait_for(parked, 1) != current
