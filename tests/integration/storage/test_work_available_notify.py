"""The triggers that tell waiting hosts there may be work, against Postgres.

A notification that is missing costs a host the rest of its wait; one that is
extra costs every idle host a claim. So both directions are asserted: queueing,
requeueing and ending a job notify, and a claim or a statement that changes no
state does not. Negative answers are made exact with a sentinel: Postgres
delivers notifications in commit order, so if the sentinel sent afterwards is
the next one heard, nothing was sent before it.
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

from cloud_agent.workers.notifications import listen
from cloud_agent.workers.work_signal import CHANNEL, WorkSignal

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


async def test_queueing_requeueing_and_ending_notify_and_claiming_does_not(store, pool):
    heard = await _listen()
    try:
        job = await _create_job(store)
        assert await heard.next() == "", "a new message is work"
        assert await _nothing_heard_since(heard, pool), "one notification per transaction"

        claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
        assert claim is not None and claim["id"] == job["id"]
        assert await _nothing_heard_since(heard, pool), "a claim makes nothing claimable"

        assert await store.attempt_commands.release_claim(
            job_id=job["id"], attempt_id=claim["attempt_id"]
        )
        assert await heard.next() == "", "a claim handed back is work again"

        claim = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
        assert claim is not None
        assert await _nothing_heard_since(heard, pool)
        assert await store.attempt_commands.transition(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="failed",
            detail="the agent exited",
        )
        assert await heard.next() == "", "an ended turn releases the follow-up behind it"

        async with pool.acquire() as conn:
            await conn.execute("UPDATE agent_jobs SET state = 'queued' WHERE FALSE")
            await conn.execute("UPDATE agent_jobs SET state = state")
        assert await _nothing_heard_since(heard, pool), "no state changed, so no work appeared"
    finally:
        await heard.connection.close()


async def test_the_triggers_go_with_a_downgrade_and_come_back_with_the_upgrade(store, pool):
    """Without them hosts are back to polling: the wait still times out."""
    config = _db_config()

    async def triggers() -> set[str]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'agent_jobs'::regclass AND NOT tgisinternal"
            )
            function = await conn.fetchval(
                "SELECT count(*) FROM pg_proc WHERE proname = 'agent_notify_work_available'"
            )
        return {row["tgname"] for row in rows} | ({"function"} if function else set())

    assert {"agent_jobs_work_queued", "agent_jobs_work_released", "function"} <= await triggers()
    # Created at head: the store reads columns a downgraded schema lacks, so
    # once downgraded the queued follow-up is written the way the schema allows.
    job = await _create_job(store)
    try:
        downgrade_to(config, "0028_host_settings")
        assert not {"agent_jobs_work_queued", "agent_jobs_work_released", "function"} & (
            await triggers()
        )
        heard = await _listen()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_jobs (id, thread_id, user_id, turn_no, parent_job_id, "
                    "task_prompt, runtime, model, state) "
                    "VALUES ($1, $2, $3, 2, $4, 'a follow-up', 'claude-code', 'glm-5.1', 'queued')",
                    f"{job['id']}_next",
                    job["thread_id"],
                    job["user_id"],
                    job["id"],
                )
            assert await _nothing_heard_since(heard, pool)
        finally:
            await heard.connection.close()
    finally:
        apply_migrations(config)
    assert {"agent_jobs_work_queued", "agent_jobs_work_released", "function"} <= await triggers()


async def test_a_queued_job_wakes_a_waiting_host_through_the_listener(store):
    config = _db_config()
    dsn = (
        f"postgresql://{quote(str(config['user']), safe='')}:"
        f"{quote(str(config['password']), safe='')}@{config['host']}:{config['port']}/"
        f"{config['database']}"
    )
    signal = WorkSignal()
    before_listening = signal.token
    listener = asyncio.create_task(listen(dsn, (signal,)))
    try:
        # The listener announces itself once it is listening.
        await asyncio.wait_for(signal.wait(before_listening, 10), 10)
        held = signal.token
        parked = asyncio.create_task(signal.wait(held, 30))
        await asyncio.sleep(0)

        await _create_job(store)

        assert await asyncio.wait_for(parked, 5) != held
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
