"""A message sent into a turn no host has taken yet is handed over when one does.

It used to look every half second for up to ten minutes. It now waits for the
job's change notification, which a claim sends; the clock is only a backstop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.api import turns
from cloud_agent.application import send_now as send_now_delivery
from cloud_agent.workers.job_event_signal import JobEventSignal

JOB = "ajob_1"
MESSAGE = {"id": "apmsg_1", "user_id": "u1", "revision": 2, "prompt": "and mind the tests"}


class Store:
    """A turn waiting for a host, and a message waiting to go into it."""

    def __init__(self) -> None:
        self.job = {"id": JOB, "state": "queued", "current_attempt_id": None}
        self.reads = 0
        self.conversation_queries = SimpleNamespace(get_job=self._get_job)
        self.pending_messages = SimpleNamespace(
            return_to_queue=AsyncMock(return_value=None),
        )

    async def _get_job(self, job_id: str) -> dict:
        self.reads += 1
        return dict(self.job)


async def test_the_claim_hands_the_message_over_at_once(monkeypatch):
    monkeypatch.setattr(send_now_delivery, "HAND_OVER_POLL_S", 30.0)
    monkeypatch.setattr(send_now_delivery, "HAND_OVER_IDLE_S", 30.0)
    handed = AsyncMock(return_value=send_now_delivery.HandoffOutcome(accepted=True))
    monkeypatch.setattr(send_now_delivery, "deliver_handoff", handed)
    store = Store()
    signal = JobEventSignal()
    signal.reset(listening=True)
    task = asyncio.create_task(
        turns._hand_over_once_claimed(
            store,  # type: ignore[arg-type]
            thread_id="athr_1",
            job_id=JOB,
            message=MESSAGE,
            gateway=None,
            app_credentials=None,
            signal=signal,
        )
    )
    await asyncio.sleep(0.05)
    assert store.reads == 1 and not handed.called, "waiting reads nothing"

    store.job.update(state="running", current_attempt_id=7)
    signal.notified(JOB)

    await asyncio.wait_for(task, 0.5)
    handed.assert_awaited_once()
    assert handed.await_args.kwargs == {
        "message_id": MESSAGE["id"],
        "revision": MESSAGE["revision"],
    }
    assert signal.subscribers(JOB) == 0


async def test_without_a_signal_it_still_looks_on_its_clock(monkeypatch):
    monkeypatch.setattr(send_now_delivery, "HAND_OVER_POLL_S", 0.01)
    handed = AsyncMock(return_value=send_now_delivery.HandoffOutcome(accepted=True))
    monkeypatch.setattr(send_now_delivery, "deliver_handoff", handed)
    store = Store()
    task = asyncio.create_task(
        turns._hand_over_once_claimed(
            store,  # type: ignore[arg-type]
            thread_id="athr_1",
            job_id=JOB,
            message=MESSAGE,
            gateway=None,
            app_credentials=None,
        )
    )
    await asyncio.sleep(0.05)
    store.job.update(state="running", current_attempt_id=7)
    await asyncio.wait_for(task, 0.5)
    handed.assert_awaited_once()


@pytest.mark.parametrize("state", ["succeeded", "cancelled"])
async def test_a_turn_that_ends_first_stops_the_wait(monkeypatch, state):
    monkeypatch.setattr(send_now_delivery, "HAND_OVER_IDLE_S", 30.0)
    handed = AsyncMock()
    monkeypatch.setattr(send_now_delivery, "deliver_handoff", handed)
    store = Store()
    signal = JobEventSignal()
    signal.reset(listening=True)
    task = asyncio.create_task(
        turns._hand_over_once_claimed(
            store,  # type: ignore[arg-type]
            thread_id="athr_1",
            job_id=JOB,
            message=MESSAGE,
            gateway=None,
            app_credentials=None,
            signal=signal,
        )
    )
    await asyncio.sleep(0.05)
    store.job["state"] = state
    signal.notified(JOB)
    await asyncio.wait_for(task, 0.5)
    handed.assert_not_awaited()
