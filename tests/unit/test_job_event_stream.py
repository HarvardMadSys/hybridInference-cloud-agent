"""An owner's event stream waits for its job's notification instead of a clock.

The stream used to read Postgres every second. Now it reads when its job
notifies (an event stored, a state changed), when the listening connection
resets, or after ``_IDLE_READ_S`` as a backstop. What has to hold:

* an event stored while the stream waits reaches it at once, not at a timeout;
* one stored *during* a read is not left for the timeout either;
* a job that ends closes its stream as soon as it says so;
* nothing listening means the old one-second clock, not a stalled stream;
* another job's notification costs this stream nothing.

The endpoint's generator is driven directly: an HTTP client test transport
collects the whole body before returning, so it cannot see when a frame left.
Replay, ``Last-Event-ID`` and frame safety stay covered in
``tests/servers/test_agent_jobs_api.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent.api import threads
from cloud_agent.domain.execution import JobStreamState
from cloud_agent.workers.job_event_signal import JobEventSignal

JOB = "ajob_1"
OWNER = {"user_id": "u1"}
#: "At once": well under the one-second clock the stream used to read on, so a
#: stream that only polled would miss it.
AT_ONCE_S = 0.5


class Queries:
    """One job and its event log, counting reads; a hook runs inside a read."""

    def __init__(self) -> None:
        self.job: dict[str, Any] = {
            "id": JOB,
            "user_id": "u1",
            "state": "running",
            "published_pr_url": None,
        }
        self.events: list[dict[str, Any]] = []
        self.reads = 0
        self.full_job_reads = 0
        self.state_reads = 0
        self.during_read: Any = None

    def store(self, text: str) -> None:
        self.events.append(
            {
                "id": len(self.events) + 1,
                "attempt_id": 7,
                "seq": len(self.events) + 1,
                "event_type": "message",
                "payload": {"text": text},
                "created_at": datetime(2026, 10, 9, tzinfo=UTC),
            }
        )

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.full_job_reads += 1
        return dict(self.job) if job_id == JOB else None

    async def get_job_stream_state(self, *, job_id: str, user_id: str) -> JobStreamState | None:
        self.state_reads += 1
        if job_id != JOB or self.job["user_id"] != user_id:
            return None
        return JobStreamState(self.job["state"], self.job["published_pr_url"])

    async def list_events_after(self, *, job_id: str, after_id: int, limit: int) -> list[Any]:
        self.reads += 1
        found = [event for event in self.events if event["id"] > after_id][:limit]
        if self.during_read is not None:
            hook, self.during_read = self.during_read, None
            hook()
        return found


class Request:
    async def is_disconnected(self) -> bool:
        return False


async def _open(queries: Queries, signal: JobEventSignal | None) -> Any:
    response = await threads.stream_agent_job_events(
        job_id=JOB,
        request=Request(),  # type: ignore[arg-type]
        last_event_id=None,
        after=0,
        user=OWNER,
        store=SimpleNamespace(conversation_queries=queries, execution_queries=queries),
        signal=signal,
    )
    return response.body_iterator


async def _settle() -> None:
    """Let the stream reach its wait."""
    await asyncio.sleep(0.05)


@pytest.fixture
def idle_read_never(monkeypatch):
    """A backstop no test can reach, so a frame on time proves a wake-up."""
    monkeypatch.setattr(threads, "_IDLE_READ_S", 30.0)
    monkeypatch.setattr(threads, "_KEEPALIVE_S", 60.0)


def _listening() -> JobEventSignal:
    signal = JobEventSignal()
    signal.reset(listening=True)
    return signal


async def test_an_event_stored_while_the_stream_waits_reaches_it_at_once(idle_read_never):
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    frame = asyncio.create_task(anext(stream))
    await _settle()
    assert not frame.done()
    assert queries.reads == 1, "waiting reads nothing"

    queries.store("one")
    signal.notified(JOB)

    assert "id: 1\nevent: message" in await asyncio.wait_for(frame, AT_ONCE_S)
    await stream.aclose()


async def test_an_event_stored_during_a_read_is_not_left_for_the_timeout(idle_read_never):
    queries = Queries()
    signal = _listening()

    def committed_meanwhile() -> None:
        queries.store("one")
        signal.notified(JOB)

    # The read has already looked; the event commits and notifies before the
    # stream begins to wait. The subscription holds the wake-up for it.
    queries.during_read = committed_meanwhile
    stream = await _open(queries, signal)

    assert "id: 1\nevent: message" in await asyncio.wait_for(anext(stream), AT_ONCE_S)
    await stream.aclose()


async def test_a_job_that_ends_closes_its_stream_when_it_says_so(idle_read_never):
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    frame = asyncio.create_task(anext(stream))
    await _settle()
    assert signal.subscribers(JOB) == 1

    queries.store("last words")
    queries.job["state"] = "succeeded"
    signal.notified(JOB)

    assert "event: message" in await asyncio.wait_for(frame, AT_ONCE_S)
    assert "event: job_finished" in await asyncio.wait_for(anext(stream), AT_ONCE_S)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert signal.subscribers(JOB) == 0, "a finished stream leaves no subscription behind"


async def test_live_wakeups_reload_only_the_minimal_state_and_finish_with_the_current_url(
    idle_read_never,
):
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    for number in range(3):
        frame = asyncio.create_task(anext(stream))
        await _settle()
        queries.store(f"message {number}")
        signal.notified(JOB)
        assert "event: message" in await asyncio.wait_for(frame, AT_ONCE_S)
    finish = asyncio.create_task(anext(stream))
    await _settle()
    queries.job.update(state="succeeded", published_pr_url="https://example.test/pr/42")
    signal.notified(JOB)
    final = await asyncio.wait_for(finish, AT_ONCE_S)
    assert "event: job_finished" in final
    assert json.loads(final.split("data: ")[1]) == {
        "state": "succeeded",
        "published_pr_url": "https://example.test/pr/42",
        "last_event_id": 3,
    }
    assert queries.full_job_reads == 1, "the full job is read only for stream admission"
    assert queries.state_reads > 1, "live wakeups still read current state"
    await stream.aclose()


async def test_another_jobs_notification_costs_this_stream_nothing(idle_read_never):
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    frame = asyncio.create_task(anext(stream))
    await _settle()

    signal.notified("ajob_2")
    await _settle()

    assert queries.reads == 1
    # A client that goes away cancels the stream mid-wait; its subscription
    # goes with it.
    frame.cancel()
    with pytest.raises(asyncio.CancelledError):
        await frame
    assert signal.subscribers(JOB) == 0


async def test_a_reset_listener_makes_every_stream_read_once(idle_read_never):
    """A notification sent while nothing listened is gone; the reset finds it."""
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    frame = asyncio.create_task(anext(stream))
    await _settle()

    queries.store("sent while the listener was down")
    signal.reset(listening=True)

    assert "id: 1\n" in await asyncio.wait_for(frame, AT_ONCE_S)
    await stream.aclose()


@pytest.mark.parametrize("signal", [None, JobEventSignal()], ids=["no signal", "not listening"])
async def test_with_nothing_listening_the_stream_reads_on_the_short_clock(
    signal, idle_read_never, monkeypatch
):
    monkeypatch.setattr(threads, "_POLL_INTERVAL_S", 0.01)
    queries = Queries()
    stream = await _open(queries, signal)
    frame = asyncio.create_task(anext(stream))
    await _settle()

    queries.store("nobody notified this")

    assert "id: 1\n" in await asyncio.wait_for(frame, 1)
    await stream.aclose()


async def test_a_backlog_longer_than_a_page_is_caught_up_without_waiting(
    idle_read_never, monkeypatch
):
    """A reconnect behind by more than a page reads on; it does not wait per page."""
    monkeypatch.setattr(threads, "_EVENT_PAGE_SIZE", 3)
    queries = Queries()
    for index in range(7):
        queries.store(f"event {index}")
    stream = await _open(queries, _listening())

    frames = [await asyncio.wait_for(anext(stream), AT_ONCE_S) for _ in range(7)]

    assert [frame.split("\n")[0] for frame in frames] == [f"id: {n}" for n in range(1, 8)]
    await stream.aclose()


async def test_an_idle_stream_sends_a_keepalive_at_the_backstop(monkeypatch):
    monkeypatch.setattr(threads, "_IDLE_READ_S", 0.05)
    monkeypatch.setattr(threads, "_KEEPALIVE_S", 0.04)
    queries = Queries()
    stream = await _open(queries, _listening())

    assert await asyncio.wait_for(anext(stream), 1) == ": keepalive\n\n"
    assert queries.reads == 2, "one read, one backstop read, nothing in between"
    await stream.aclose()
