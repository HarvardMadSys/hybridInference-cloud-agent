"""One account may hold only so many event streams open on one process.

Each open change stream or job event stream keeps reading Postgres for as long
as it lives. What has to hold:

* the stream past the ceiling is refused with a 429 the web app backs off on;
* another account is not affected by this one's streams;
* a slot comes back however its stream ends: run out, closed by the client,
  failed, or never started at all;
* the ceiling is configurable, and an unusable value is the default, never
  "unlimited" or "none".
"""

from __future__ import annotations

import gc
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from cloud_agent.api import _stream_slots, changes, threads
from cloud_agent.api._stream_slots import (
    DEFAULT_MAX_STREAMS_PER_USER,
    ENV_MAX_STREAMS_PER_USER,
    STREAM_SLOTS,
    StreamSlots,
    max_streams_per_user,
)
from cloud_agent.domain.execution import JobStreamState
from cloud_agent.workers.owner_change_signal import OwnerChangeSignal


@pytest.fixture(autouse=True)
def two_streams(monkeypatch):
    monkeypatch.setenv(ENV_MAX_STREAMS_PER_USER, "2")


async def _items(*values: Any):
    for value in values:
        yield value


def test_the_stream_past_the_ceiling_is_refused_and_others_are_not():
    slots = StreamSlots()
    held = [slots.acquire("owner"), slots.acquire("owner")]
    with pytest.raises(HTTPException) as refused:
        slots.acquire("owner")
    assert refused.value.status_code == 429
    assert refused.value.headers == {"Retry-After": "5"}
    assert refused.value.detail["error"]["type"] == "too_many_streams"
    assert slots.acquire("someone-else")
    held[0].release()
    held[0].release()  # idempotent: one stream gives back one slot
    assert slots.open_for("owner") == 1
    assert slots.acquire("owner")


async def test_a_slot_comes_back_when_its_stream_runs_out_or_is_closed():
    slots = StreamSlots()
    assert [item async for item in slots.acquire("owner").hold(_items(1, 2))] == [1, 2]
    assert slots.open_for("owner") == 0

    closed = []

    async def endless():
        try:
            while True:
                yield "frame"
        finally:
            closed.append(True)

    stream = slots.acquire("owner").hold(endless())
    assert await anext(stream) == "frame"
    assert slots.open_for("owner") == 1
    await stream.aclose()
    assert closed == [True], "the wrapped stream is closed with its slot"
    assert slots.open_for("owner") == 0


async def test_a_slot_comes_back_when_its_stream_fails():
    slots = StreamSlots()

    async def failing():
        yield "frame"
        raise RuntimeError("database went away")

    stream = slots.acquire("owner").hold(failing())
    await anext(stream)
    with pytest.raises(RuntimeError):
        await anext(stream)
    assert slots.open_for("owner") == 0


def test_a_slot_whose_stream_never_started_is_returned_when_collected():
    """The client left before the first byte: no ``finally`` ever runs."""
    slots = StreamSlots()
    stream = slots.acquire("owner").hold(_items(1))
    assert slots.open_for("owner") == 1
    del stream
    gc.collect()
    assert slots.open_for("owner") == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", DEFAULT_MAX_STREAMS_PER_USER), ("7", 7), ("0", DEFAULT_MAX_STREAMS_PER_USER)],
)
def test_the_ceiling_is_configurable_and_never_unbounded(monkeypatch, raw, expected):
    monkeypatch.setenv(ENV_MAX_STREAMS_PER_USER, raw)
    assert max_streams_per_user() == expected
    monkeypatch.setenv(ENV_MAX_STREAMS_PER_USER, "many")
    assert max_streams_per_user() == DEFAULT_MAX_STREAMS_PER_USER


class _Request:
    async def is_disconnected(self) -> bool:
        return False


async def _change_stream(user_id: str) -> Any:
    signal = OwnerChangeSignal()
    response = await changes.stream_owner_changes(
        request=_Request(),  # type: ignore[arg-type]
        user={"user_id": user_id},
        store=SimpleNamespace(conversation_queries=None),
        signal=signal,
    )
    return response.body_iterator


async def test_the_change_stream_route_counts_its_owner_s_streams():
    owner = "stream-slots-change-owner"
    first, second = await _change_stream(owner), await _change_stream(owner)
    with pytest.raises(HTTPException) as refused:
        await _change_stream(owner)
    assert refused.value.status_code == 429
    other = await _change_stream("stream-slots-change-other")
    await anext(first)
    await first.aclose()
    third = await _change_stream(owner)
    for stream in (second, other, third):
        await stream.aclose()
    gc.collect()
    assert STREAM_SLOTS.open_for(owner) == 0


class _Jobs:
    async def get_job(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "user_id": "stream-slots-job-owner", "deleted_at": None}

    async def get_job_stream_state(self, *, job_id: str, user_id: str) -> JobStreamState:
        return JobStreamState("running", None)

    async def list_events_after(self, *, job_id: str, after_id: int, limit: int) -> list[Any]:
        return []


async def _job_stream(job_id: str, user_id: str = "stream-slots-job-owner") -> Any:
    response = await threads.stream_agent_job_events(
        job_id=job_id,
        request=_Request(),  # type: ignore[arg-type]
        last_event_id=None,
        after=0,
        user={"user_id": user_id},
        store=SimpleNamespace(conversation_queries=_Jobs(), execution_queries=_Jobs()),
        signal=None,
    )
    return response.body_iterator


async def test_the_job_stream_route_shares_the_same_count():
    owner = "stream-slots-job-owner"
    first, second = await _job_stream("ajob_1"), await _job_stream("ajob_2")
    with pytest.raises(HTTPException) as refused:
        await _job_stream("ajob_3")
    assert refused.value.status_code == 429
    # Ownership is decided first: someone else's job is still a 404, not a 429.
    with pytest.raises(HTTPException) as hidden:
        await _job_stream("ajob_4", user_id="stream-slots-intruder")
    assert hidden.value.status_code == 404
    for stream in (first, second):
        await stream.aclose()
    gc.collect()
    assert STREAM_SLOTS.open_for(owner) == 0
    assert _stream_slots.STREAM_SLOTS is STREAM_SLOTS
