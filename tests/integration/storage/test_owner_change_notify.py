"""Row revisions and the triggers that push owners' changes, against Postgres.

A missing notification leaves a tab showing an old state, title or link until
its fallback read; an extra one costs it a read. A revision that does not rise
on a change lets an older value replace a newer one; one that rises without a
change makes every tab read for nothing. So both directions are asserted, and
negative answers are made exact with a sentinel: Postgres delivers
notifications in commit order, so if the sentinel sent afterwards is the next
one heard, nothing was sent before it.
"""

from __future__ import annotations

import asyncio
import json
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
from cloud_agent.workers.owner_change_signal import CHANNEL, OwnerChangeSignal

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_SENTINEL = "sentinel"


class Heard:
    """A connection listening on the channel, and what it has heard so far."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection
        self.payloads: asyncio.Queue[str] = asyncio.Queue()

    async def next(self) -> dict | str:
        payload = await asyncio.wait_for(self.payloads.get(), 5)
        return payload if payload == _SENTINEL else json.loads(payload)

    async def quiet(self, pool) -> bool:
        """True when nothing was notified before a sentinel sent now."""
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, _SENTINEL)
        return await self.next() == _SENTINEL


async def _listen() -> Heard:
    heard = Heard(await asyncpg.connect(**_db_config()))
    await heard.connection.add_listener(
        CHANNEL, lambda _c, _pid, _channel, payload: heard.payloads.put_nowait(payload)
    )
    return heard


async def _revisions(pool, job_id: str) -> tuple[int, int]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT j.revision AS job, t.revision AS thread FROM agent_jobs j "
            "JOIN agent_threads t ON t.id = j.thread_id WHERE j.id = $1",
            job_id,
        )
    return row["job"], row["thread"]


async def test_changes_raise_the_revision_and_notify_the_owner(store, pool):
    heard = await _listen()
    try:
        job = await _create_job(store)
        user, thread = job["user_id"], job["thread_id"]
        created = [await heard.next(), await heard.next()]
        assert sorted(created, key=str) == sorted(
            [
                {"user": user, "kind": "thread", "id": thread},
                {"user": user, "kind": "job", "id": job["id"]},
            ],
            key=str,
        ), "a new conversation and its first turn"
        assert await heard.quiet(pool)
        assert await _revisions(pool, job["id"]) == (0, 0)

        claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
        assert claim is not None
        assert await heard.next() == {"user": user, "kind": "job", "id": job["id"]}
        job_revision, thread_revision = await _revisions(pool, job["id"])
        assert job_revision > 0

        assert await store.conversation_commands.set_thread_title(
            job_id=job["id"], user_id=user, title="A better title"
        )
        assert await heard.next() == {"user": user, "kind": "thread", "id": thread}
        assert await heard.quiet(pool)
        assert (await _revisions(pool, job["id"]))[1] > thread_revision

        # Rewriting a row to the values it already has changes nothing.
        before = await _revisions(pool, job["id"])
        async with pool.acquire() as conn:
            await conn.execute("UPDATE agent_jobs SET state = state")
            await conn.execute("UPDATE agent_threads SET title = title")
        assert await heard.quiet(pool), "no change, no notification"
        assert await _revisions(pool, job["id"]) == before, "no change, no new revision"

        # A row that is removed is reported too, so a tab can drop it.
        queued = await _create_job(store, task_prompt="a second chat")
        await heard.next()
        await heard.next()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM agent_thread_messages WHERE job_id = $1", queued["id"])
            await conn.execute("DELETE FROM agent_jobs WHERE id = $1", queued["id"])
        assert await heard.next() == {"user": user, "kind": "job", "id": queued["id"]}
    finally:
        await heard.connection.close()


async def test_the_owner_reads_current_values_and_only_their_own(store):
    job = await _create_job(store)
    other = await _create_job(store, user_id="user-2")
    await store.conversation_commands.set_thread_title(
        job_id=job["id"], user_id=job["user_id"], title="Renamed"
    )

    rows = await store.conversation_queries.owner_changes(
        user_id=job["user_id"],
        job_ids=[job["id"], other["id"]],
        thread_ids=[job["thread_id"], other["thread_id"]],
    )

    assert [row["id"] for row in rows["jobs"]] == [job["id"]]
    assert rows["jobs"][0]["state"] == "queued"
    assert rows["jobs"][0]["conversation_state"] == "queued"
    assert [row["id"] for row in rows["threads"]] == [job["thread_id"]]
    assert rows["threads"][0]["title"] == "Renamed"
    assert rows["threads"][0]["revision"] > 0

    read = await store.conversation_queries.get_job(job["id"])
    assert read is not None
    assert read["thread_revision"] == rows["threads"][0]["revision"]
    assert read["revision"] == rows["jobs"][0]["revision"]


async def test_the_change_goes_with_a_downgrade_and_comes_back_with_the_upgrade(store, pool):
    config = _db_config()

    async def installed() -> set[str]:
        async with pool.acquire() as conn:
            triggers = await conn.fetch(
                "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
                "('agent_jobs'::regclass, 'agent_threads'::regclass) AND NOT tgisinternal"
            )
            columns = await conn.fetch(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'revision' AND table_name IN ('agent_jobs', 'agent_threads')"
            )
        return {row["tgname"] for row in triggers} | {
            f"{row['table_name']}.revision" for row in columns
        }

    ours = {
        f"{table}_{name}"
        for table in ("agent_jobs", "agent_threads")
        for name in ("bump_revision", "owner_inserted", "owner_changed", "owner_deleted")
    } | {"agent_jobs.revision", "agent_threads.revision"}
    assert ours <= await installed()
    try:
        downgrade_to(config, "0030_job_event_notify")
        assert not ours & await installed()
        assert "agent_jobs_state_changed" in await installed(), "0030's trigger stays"
    finally:
        apply_migrations(config)
    assert ours <= await installed()


async def test_a_change_reaches_the_owners_subscription_through_the_shared_listener(store):
    config = _db_config()
    dsn = (
        f"postgresql://{quote(str(config['user']), safe='')}:"
        f"{quote(str(config['password']), safe='')}@{config['host']}:{config['port']}/"
        f"{config['database']}"
    )
    job = await _create_job(store)
    signal = OwnerChangeSignal()
    listener = asyncio.create_task(listen(dsn, (signal,)))
    try:
        with signal.subscribe(job["user_id"]) as subscription:
            # The listener resets every subscription once it is listening.
            assert await subscription.wait(10)
            assert subscription.take().resync and signal.listening

            await store.conversation_commands.set_thread_title(
                job_id=job["id"], user_id=job["user_id"], title="Pushed"
            )

            assert await subscription.wait(5)
            assert subscription.take().threads == {job["thread_id"]}
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
