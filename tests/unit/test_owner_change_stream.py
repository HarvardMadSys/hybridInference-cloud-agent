"""An owner's change stream: one connection per tab, pushed on change.

What has to hold:

* a tab is told ``hello`` only once its changes are being collected, so a list
  it reads after that misses nothing;
* a changed job or thread reaches it at once, with its revision and values;
* a job's changed usage reaches it with its value; the queue, tool approvals and
  publication progress as hints to read again;
* a row that is gone is reported as deleted, not silently skipped;
* another owner's change costs this stream nothing;
* a reset of the listening connection tells it to resynchronise, and whether
  pushes are flowing;
* where nothing pushes, the endpoint says so (404) and the tab polls.

The endpoint's generator is driven directly: an HTTP client test transport
collects the whole body before returning.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from cloud_agent.api import changes
from cloud_agent.workers.owner_change_signal import OwnerChangeSignal

OWNER = "u1"
AT_ONCE_S = 0.5


class Queries:
    """The rows the owner holds, and the reads made of them."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.reads: list[tuple[list[str], list[str]]] = []

    async def owner_changes(
        self, *, user_id: str, job_ids: list[str], thread_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        assert user_id == OWNER
        self.reads.append((job_ids, thread_ids))
        return {
            "jobs": [self.jobs[i] for i in job_ids if i in self.jobs],
            "threads": [self.threads[i] for i in thread_ids if i in self.threads],
        }


class Request:
    async def is_disconnected(self) -> bool:
        return False


def _notify(signal: OwnerChangeSignal, kind: str, row_id: str, user: str = OWNER) -> None:
    signal.notified(json.dumps({"user": user, "kind": kind, "id": row_id}))


def _parse(frame: str) -> tuple[str, dict[str, Any]]:
    event, data = frame.strip().split("\n")
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


async def _open(queries: Queries, signal: OwnerChangeSignal | None) -> Any:
    response = await changes.stream_owner_changes(
        request=Request(),  # type: ignore[arg-type]
        user={"user_id": OWNER},
        store=SimpleNamespace(conversation_queries=queries),
        signal=signal,
    )
    return response.body_iterator


def _listening() -> OwnerChangeSignal:
    signal = OwnerChangeSignal()
    signal.reset(listening=True)
    return signal


@pytest.fixture(autouse=True)
def no_keepalive(monkeypatch):
    monkeypatch.setattr(changes, "_KEEPALIVE_S", 30.0)


async def test_hello_comes_once_changes_are_collected_and_says_whether_pushes_flow():
    signal = _listening()
    stream = await _open(Queries(), signal)
    assert _parse(await asyncio.wait_for(anext(stream), AT_ONCE_S)) == ("hello", {"live": True})
    assert signal.subscribers(OWNER) == 1
    await stream.aclose()
    assert signal.subscribers(OWNER) == 0

    stream = await _open(Queries(), OwnerChangeSignal())
    assert _parse(await asyncio.wait_for(anext(stream), AT_ONCE_S)) == ("hello", {"live": False})
    await stream.aclose()


async def test_a_changed_job_and_thread_reach_the_tab_at_once_with_their_revisions():
    queries = Queries()
    queries.jobs["ajob_1"] = {
        "id": "ajob_1",
        "thread_id": "athr_1",
        "revision": 4,
        "state": "succeeded",
        "conversation_state": "idle",
        "published_pr_url": "https://example.test/pr/1",
    }
    queries.threads["athr_1"] = {
        "id": "athr_1",
        "revision": 7,
        "title": "Fix the flaky test",
        "title_source": "generated",
        "pinned_at": None,
        "archived_at": None,
        "deleted_at": None,
        "updated_at": datetime(2026, 10, 10, tzinfo=UTC),
    }
    signal = _listening()
    stream = await _open(queries, signal)
    await anext(stream)
    frame = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    assert not frame.done() and queries.reads == [], "waiting reads nothing"

    _notify(signal, "job", "ajob_1")
    _notify(signal, "thread", "athr_1")

    assert _parse(await asyncio.wait_for(frame, AT_ONCE_S)) == (
        "thread",
        {
            "id": "athr_1",
            "revision": 7,
            "title": "Fix the flaky test",
            "title_source": "generated",
            "pinned_at": None,
            "archived_at": None,
            "deleted_at": None,
            "updated_at": "2026-10-10T00:00:00+00:00",
        },
    )
    assert _parse(await asyncio.wait_for(anext(stream), AT_ONCE_S)) == (
        "job",
        {
            "id": "ajob_1",
            "thread_id": "athr_1",
            "revision": 4,
            "state": "succeeded",
            "conversation_state": "idle",
            "published_pr_url": "https://example.test/pr/1",
        },
    )
    assert queries.reads == [(["ajob_1"], ["athr_1"])], "one read for both"
    await stream.aclose()


async def test_usage_arrives_with_its_value_and_the_rest_as_hints(monkeypatch):
    """Usage is read once by the stream and sent; the others name what to read again."""
    usage = {"ledger": {"status": "available"}, "runtime": {"source": "runtime"}, "version": "v"}

    async def owner_usage(store: Any, *, job_id: str, user_id: str) -> Any:
        if job_id != "ajob_1":
            return None  # not this owner's, or gone
        return SimpleNamespace(model_dump=lambda **_: usage)

    read_usage = AsyncMock(side_effect=owner_usage)
    monkeypatch.setattr(changes, "job_usage_response", read_usage)
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    await anext(stream)

    _notify(signal, "usage", "ajob_1")
    _notify(signal, "usage", "ajob_gone")
    _notify(signal, "mcp", "ajob_1")
    _notify(signal, "queue", "athr_1")
    _notify(signal, "publication", "athr_1")
    _notify(signal, "something-new", "x")

    frames = [_parse(await asyncio.wait_for(anext(stream), AT_ONCE_S)) for _ in range(5)]
    assert frames == [
        ("usage", {"id": "ajob_1", "usage": usage}),
        ("usage", {"id": "ajob_gone"}),
        ("queue", {"id": "athr_1"}),
        ("mcp", {"id": "ajob_1"}),
        ("publication", {"id": "athr_1"}),
    ]
    # Read as this owner, so another owner's job reads as nothing.
    assert [call.kwargs for call in read_usage.await_args_list] == [
        {"job_id": "ajob_1", "user_id": OWNER},
        {"job_id": "ajob_gone", "user_id": OWNER},
    ]
    assert queries.reads == [], "no row read for them"
    await stream.aclose()


async def test_a_row_that_is_gone_is_reported_as_deleted():
    signal = _listening()
    stream = await _open(Queries(), signal)
    await anext(stream)
    _notify(signal, "thread", "athr_gone")
    assert _parse(await asyncio.wait_for(anext(stream), AT_ONCE_S)) == (
        "thread",
        {"id": "athr_gone", "deleted": True},
    )
    await stream.aclose()


async def test_another_owners_change_costs_this_stream_nothing():
    queries = Queries()
    signal = _listening()
    stream = await _open(queries, signal)
    await anext(stream)
    frame = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)

    _notify(signal, "job", "ajob_9", user="someone-else")
    signal.notified("not json")
    await asyncio.sleep(0.05)

    assert queries.reads == [] and not frame.done()
    frame.cancel()
    with pytest.raises(asyncio.CancelledError):
        await frame
    assert signal.subscribers(OWNER) == 0, "a closed tab leaves no subscription behind"


@pytest.mark.parametrize("listening", [True, False])
async def test_a_reset_listener_tells_the_tab_to_resynchronise(listening):
    signal = _listening()
    stream = await _open(Queries(), signal)
    await anext(stream)
    frame = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)

    signal.reset(listening=listening)

    assert _parse(await asyncio.wait_for(frame, AT_ONCE_S)) == ("resync", {"live": listening})
    await stream.aclose()


async def test_an_idle_stream_sends_keepalives(monkeypatch):
    monkeypatch.setattr(changes, "_KEEPALIVE_S", 0.05)
    queries = Queries()
    stream = await _open(queries, _listening())
    await anext(stream)
    assert await asyncio.wait_for(anext(stream), AT_ONCE_S) == ": keepalive\n\n"
    assert queries.reads == []
    await stream.aclose()


async def test_where_nothing_pushes_the_tab_is_told_to_poll():
    with pytest.raises(HTTPException) as refused:
        await _open(Queries(), None)
    assert refused.value.status_code == 404
