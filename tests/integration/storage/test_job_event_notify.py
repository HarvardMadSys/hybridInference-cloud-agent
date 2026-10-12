"""The triggers that wake an owner's event stream, against Postgres.

A missing notification costs a stream its backstop wait; an extra one costs it
a read. So both directions are asserted: storing an event and changing state
notify with the job's id, and a statement that changes no state does not.
Negative answers are made exact with a sentinel: Postgres delivers
notifications in commit order, so if the sentinel sent afterwards is the next
one heard, nothing was sent before it.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.workers.job_event_signal import CHANNEL, JobEventSignal
from cloud_agent.workers.notifications import listen

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_SENTINEL = "sentinel"


class Heard:
    """A connection listening on the channel, and what it has heard so far."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection
        self.payloads: asyncio.Queue[str] = asyncio.Queue()

    async def next(self) -> str:
        return await asyncio.wait_for(self.payloads.get(), 5)


async def _listen() -> Heard:
    heard = Heard(await asyncpg.connect(**_db_config()))
    await heard.connection.add_listener(
        CHANNEL, lambda _c, _pid, _channel, payload: heard.payloads.put_nowait(payload)
    )
    return heard


async def _nothing_heard_since(heard: Heard, pool) -> bool:
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, _SENTINEL)
    return await heard.next() == _SENTINEL


async def test_events_and_state_changes_notify_with_the_job_id(store, pool):
    job = await _create_job(store)
    heard = await _listen()
    try:
        claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
        assert claim is not None and claim["id"] == job["id"]
        assert await heard.next() == job["id"], "queued to running is a change of state"
        assert await _nothing_heard_since(heard, pool)

        assert await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"text": "hello"},
        )
        assert await heard.next() == job["id"], "a stored event"
        assert await _nothing_heard_since(heard, pool)

        # Rows of one job in one transaction fold into one notification.
        async with pool.acquire() as conn, conn.transaction():
            for seq in (100, 101, 102):
                await conn.execute(
                    "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                    " VALUES ($1, $2, $3, 'message', '{}'::jsonb)",
                    job["id"],
                    claim["attempt_id"],
                    seq,
                )
        assert await heard.next() == job["id"]
        assert await _nothing_heard_since(heard, pool), "one notification per transaction"

        async with pool.acquire() as conn:
            await conn.execute("UPDATE agent_jobs SET state = state")
        assert await _nothing_heard_since(heard, pool), "no state changed, so nothing to read"

        assert await store.attempt_commands.transition(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="failed",
            detail="the agent exited",
        )
        assert await heard.next() == job["id"], "an ended job closes its stream"
    finally:
        await heard.connection.close()


async def test_the_triggers_go_with_a_downgrade_and_come_back_with_the_upgrade(store, pool):
    """Without them the code before this revision reads on its own clock."""
    config = _db_config()

    async def installed() -> set[str]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
                "('agent_jobs'::regclass, 'agent_job_events'::regclass) AND NOT tgisinternal"
            )
            functions = await conn.fetch(
                "SELECT proname FROM pg_proc WHERE proname IN "
                "('agent_notify_job_event_stored', 'agent_notify_job_state_changed')"
            )
        return {row["tgname"] for row in rows} | {row["proname"] for row in functions}

    ours = {
        "agent_job_events_stored",
        "agent_jobs_state_changed",
        "agent_notify_job_event_stored",
        "agent_notify_job_state_changed",
    }
    assert ours <= await installed()
    # Claimed at head: the store reads columns a downgraded schema lacks, so
    # once downgraded the event and the state change are written directly.
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    try:
        downgrade_to(config, "0029_work_available_notify")
        assert not ours & await installed()
        assert {"agent_jobs_work_queued", "agent_jobs_work_released"} <= await installed()
        heard = await _listen()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type) "
                    "VALUES ($1, $2, 1, 'message')",
                    job["id"],
                    claim["attempt_id"],
                )
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'failed' WHERE id = $1", job["id"]
                )
            assert await _nothing_heard_since(heard, pool)
        finally:
            await heard.connection.close()
    finally:
        apply_migrations(config)
    assert ours <= await installed()


async def test_a_stored_event_wakes_its_stream_through_the_shared_listener(store):
    config = _db_config()
    dsn = (
        f"postgresql://{quote(str(config['user']), safe='')}:"
        f"{quote(str(config['password']), safe='')}@{config['host']}:{config['port']}/"
        f"{config['database']}"
    )
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    signal = JobEventSignal()
    listener = asyncio.create_task(listen(dsn, (signal,)))
    try:
        with signal.subscribe(job["id"]) as woken:
            # The listener resets every stream once it is listening.
            assert await signal.wait(woken, 10)
            assert signal.listening

            assert await store.attempt_commands.append_event(
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                event_type="message",
                payload={"text": "hello"},
            )

            assert await signal.wait(woken, 5)
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
