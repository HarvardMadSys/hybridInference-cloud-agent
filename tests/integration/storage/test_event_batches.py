"""A runner's batch of events, appended in one transaction on Postgres.

The batch endpoint stores what a runner coalesced: one fence check and one
commit, so one wake-up of the owner's stream, for the whole batch, each event
stored as a single append stores it. Each of those is a property of the SQL and
of the transaction around it, which a fake cannot have: the batch is one
transaction (one notification, and all of it or none), a batch sent again whole
stores nothing twice, its side effects run in its order, and a lost lease
stores none of it. Negative answers are made exact with a sentinel: Postgres
delivers notifications in commit order, so if the sentinel sent afterwards is
the next one heard, nothing was sent before it.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _expire_attempt,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config

from cloud_agent.storage._sql import event_batches
from cloud_agent.workers.job_event_signal import CHANNEL as JOB_EVENTS
from cloud_agent.workers.owner_change_signal import CHANNEL as OWNER

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_SENTINEL = "sentinel"


class Heard:
    """A connection listening on one channel, and what it has heard so far."""

    def __init__(self, connection: asyncpg.Connection, channel: str) -> None:
        self.connection = connection
        self.channel = channel
        self.payloads: asyncio.Queue[str] = asyncio.Queue()

    async def until_quiet(self, pool) -> list[str]:
        """Everything heard before a sentinel sent now, in order."""
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", self.channel, _SENTINEL)
        heard = []
        while (payload := await asyncio.wait_for(self.payloads.get(), 5)) != _SENTINEL:
            heard.append(payload)
        return heard


async def _listen(channel: str) -> Heard:
    heard = Heard(await asyncpg.connect(**_db_config()), channel)
    await heard.connection.add_listener(
        channel, lambda _c, _pid, _channel, payload: heard.payloads.put_nowait(payload)
    )
    return heard


async def _claimed(store) -> tuple[dict, dict]:
    """A job and the fence of the attempt claiming it."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == job["id"]
    return job, {"attempt_id": claim["attempt_id"], "lease_generation": claim["lease_generation"]}


async def _stored(pool, attempt_id: int) -> list[tuple[int, int | None, str]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seq, client_seq, event_type FROM agent_job_events "
            "WHERE attempt_id = $1 ORDER BY seq",
            attempt_id,
        )
    return [(row["seq"], row["client_seq"], row["event_type"]) for row in rows]


async def _assistant_messages(pool, job_id: str) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content FROM agent_thread_messages "
            "WHERE job_id = $1 AND role = 'assistant' ORDER BY id",
            job_id,
        )
    return [row["content"] for row in rows]


async def test_a_batch_is_stored_in_order_in_one_transaction(store, pool):
    job, fence = await _claimed(store)
    heard = await _listen(JOB_EVENTS)
    try:
        event_ids = await store.attempt_commands.append_events(
            **fence,
            events=[
                ("lifecycle", {"phase": "turn_started"}, 1),
                ("message", {"text": "Reading the failing test."}, 2),
                ("tool_use", {"name": "ls"}, 3),
            ],
        )
        assert await heard.until_quiet(pool) == [job["id"]], "one wake-up for the whole batch"
    finally:
        await heard.connection.close()

    assert event_ids is not None
    assert event_ids == sorted(set(event_ids)) and len(event_ids) == 3
    assert await _stored(pool, fence["attempt_id"]) == [
        (1, 1, "lifecycle"),
        (2, 2, "message"),
        (3, 3, "tool_use"),
    ]
    assert await _assistant_messages(pool, job["id"]) == ["Reading the failing test."]


async def test_a_batch_that_fails_part_way_stores_none_of_it(store, pool, monkeypatch):
    job, fence = await _claimed(store)
    original = event_batches.insert_event

    async def refuse_the_second(conn, **fields):
        if fields["client_seq"] == 2:
            raise RuntimeError("the second event could not be stored")
        return await original(conn, **fields)

    monkeypatch.setattr(event_batches, "insert_event", refuse_the_second)
    heard = await _listen(JOB_EVENTS)
    try:
        with pytest.raises(RuntimeError, match="second event"):
            await store.attempt_commands.append_events(
                **fence,
                events=[("message", {"text": "first"}, 1), ("message", {"text": "second"}, 2)],
            )
        assert await heard.until_quiet(pool) == [], "nothing committed, so nothing to read"
    finally:
        await heard.connection.close()

    assert await _stored(pool, fence["attempt_id"]) == []
    assert await _assistant_messages(pool, job["id"]) == []


async def test_a_batch_sent_again_whole_stores_nothing_twice(store, pool):
    """A lost answer is retried whole, and a control plane rolled back gets the same numbers."""
    job, fence = await _claimed(store)
    batch = [("message", {"text": "the answer"}, 1), ("tool_use", {"name": "ls"}, 2)]

    first = await store.attempt_commands.append_events(**fence, events=batch)
    replay = await store.attempt_commands.append_events(**fence, events=batch)
    overlap = await store.attempt_commands.append_events(
        **fence, events=[batch[1], ("message", {"text": "and more"}, 3)]
    )
    single = await store.attempt_commands.append_event(
        **fence, event_type="message", payload={"text": "and more"}, event_seq=3
    )

    assert first is not None and overlap is not None
    assert replay == first
    assert overlap[0] == first[1] and overlap[1] not in first
    assert single == overlap[1]
    assert await _stored(pool, fence["attempt_id"]) == [
        (1, 1, "message"),
        (2, 2, "tool_use"),
        (3, 3, "message"),
    ]
    assert await _assistant_messages(pool, job["id"]) == ["the answer", "and more"]


async def test_a_batchs_side_effects_run_in_its_order(store, pool):
    """Terminal readiness follows each phase in turn; usage tells the owner once."""
    job, fence = await _claimed(store)
    usage = {"input_tokens": 3, "output_tokens": 1}

    async def terminal_ready() -> bool:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT terminal_ready FROM agent_attempts WHERE id = $1", fence["attempt_id"]
            )

    owner = await _listen(OWNER)

    async def usage_hints() -> list[str]:
        heard = [json.loads(payload) for payload in await owner.until_quiet(pool)]
        return [item["id"] for item in heard if item["kind"] == "usage"]

    try:
        await owner.until_quiet(pool)
        await store.attempt_commands.append_events(
            **fence,
            events=[
                ("lifecycle", {"phase": "workspace_preparing"}, 1),
                ("message", {"text": "one", "usage": usage}, 2),
                ("lifecycle", {"phase": "workspace_ready"}, 3),
                ("message", {"text": "two", "usage": usage}, 4),
            ],
        )
        assert await usage_hints() == [job["id"]]
        assert await terminal_ready() is True

        await store.attempt_commands.append_events(
            **fence,
            events=[
                ("lifecycle", {"phase": "workspace_ready"}, 5),
                ("lifecycle", {"phase": "workspace_finalizing"}, 6),
            ],
        )
        assert await usage_hints() == [], "no usage, no hint"
        assert await terminal_ready() is False
    finally:
        await owner.connection.close()


async def test_a_batch_without_its_lease_stores_none_of_it(store, pool):
    _job, fence = await _claimed(store)
    stale = {**fence, "lease_generation": fence["lease_generation"] + 1}
    batch = [("message", {"text": "late"}, 1)]

    assert await store.attempt_commands.append_events(**stale, events=batch) is None
    await _expire_attempt(pool, store, fence["attempt_id"])
    assert await store.attempt_commands.append_events(**fence, events=batch) is None
    assert await _stored(pool, fence["attempt_id"]) == []


async def test_a_batch_refreshes_the_thread_stats_read_before_it(store, pool):
    """An attempt's stored stats rollup is reduced again once a batch adds to it.

    A thread's stats reuse an attempt's rollup while the attempt's newest
    sequence is the one it was reduced at. A batch numbers its events as single
    appends do, past every stored one, so the next read counts them.
    """
    job, fence = await _claimed(store)
    assert await store.attempt_commands.append_events(
        **fence, events=[("message", {"text": "Looking."}, 1)]
    )
    before = await store.execution_queries.thread_stats(job_id=job["id"], user_id=job["user_id"])
    assert before is not None and before["stats"].tool_calls == 0

    assert await store.attempt_commands.append_events(
        **fence,
        events=[("tool_use", {"id": "call-1", "name": "ls"}, 2), ("tool_use", {"id": "call-2"}, 3)],
    )
    after = await store.execution_queries.thread_stats(job_id=job["id"], user_id=job["user_id"])

    assert after is not None and after["stats"].tool_calls == 2
    async with pool.acquire() as conn:
        reduced_at = await conn.fetchval(
            "SELECT event_seq FROM agent_attempt_thread_stats WHERE attempt_id = $1",
            fence["attempt_id"],
        )
    assert reduced_at == 3
