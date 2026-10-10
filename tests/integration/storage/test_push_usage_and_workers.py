"""Usage, queue, approval and publication hints, and the workers' wake-ups, on Postgres.

Migration ``0032``. Owners' tabs are told, by job or conversation, when usage,
the message queue, a tool approval or publication progress changed; the
control plane's snapshot, publish and usage workers are told when there may be
work for them. Both directions are asserted where a wrong answer costs
something: a capture the coordinator deferred must *not* wake it, or a busy
host is retried in a loop. Negative answers are made exact with a sentinel:
Postgres delivers notifications in commit order, so if the sentinel sent
afterwards is the next one heard, nothing was sent before it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_shared_chat,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_mcp_connection_store import _connect, _invocation
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.workers.background_wake import CHANNEL as BACKGROUND
from cloud_agent.workers.owner_change_signal import CHANNEL as OWNER

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_SENTINEL = "sentinel"


class Heard:
    """A connection listening on one channel, and what it has heard so far."""

    def __init__(self, connection: asyncpg.Connection, channel: str) -> None:
        self.connection = connection
        self.channel = channel
        self.payloads: asyncio.Queue[str] = asyncio.Queue()

    async def next(self) -> str:
        return await asyncio.wait_for(self.payloads.get(), 5)

    async def until_quiet(self, pool) -> list[str]:
        """Everything heard before a sentinel sent now, in order."""
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", self.channel, _SENTINEL)
        heard = []
        while (payload := await self.next()) != _SENTINEL:
            heard.append(payload)
        return heard


async def _listen(channel: str) -> Heard:
    heard = Heard(await asyncpg.connect(**_db_config()), channel)
    await heard.connection.add_listener(
        channel, lambda _c, _pid, _channel, payload: heard.payloads.put_nowait(payload)
    )
    return heard


def _owner_hints(payloads: list[str], kind: str) -> list[tuple[str, str]]:
    found = [json.loads(payload) for payload in payloads]
    return [(item["user"], item["id"]) for item in found if item["kind"] == kind]


async def _ledger_row(pool, attempt_id: int) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO agent_attempt_ledger "
            "(attempt_id, grant_id, residency_generation, binding_id, window_since) "
            "VALUES ($1, 'grant-one', 1, 'gateway-binding-one', NOW()) RETURNING id",
            attempt_id,
        )


async def test_a_model_calls_usage_is_read_at_once_and_pushed_to_its_owner(store, pool):
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    owner = await _listen(OWNER)
    background = await _listen(BACKGROUND)
    try:
        ledger_id = await _ledger_row(pool, claim["attempt_id"])
        assert _owner_hints(await owner.until_quiet(pool), "usage") == [
            (job["user_id"], job["id"])
        ], "attribution became available"

        # An event without usage asks for nothing.
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="tool_result",
            payload={"content": "ok"},
        )
        assert await background.until_quiet(pool) == []

        # A model call's usage schedules a read, which wakes the collector.
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}},
        )
        assert await background.until_quiet(pool) == ["ledger"]
        async with pool.acquire() as conn:
            scheduled = await conn.fetchval(
                "SELECT next_refresh_at <= NOW() FROM agent_attempt_ledger WHERE id = $1",
                ledger_id,
            )
        assert scheduled is True

        # The read's result reaches the owner; bookkeeping alone does not.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_attempt_ledger SET refresh_generation = refresh_generation + 1 "
                "WHERE id = $1",
                ledger_id,
            )
            assert _owner_hints(await owner.until_quiet(pool), "usage") == []
            await conn.execute(
                "UPDATE agent_attempt_ledger SET snapshot = '{}'::jsonb, queried_at = NOW() "
                "WHERE id = $1",
                ledger_id,
            )
        assert _owner_hints(await owner.until_quiet(pool), "usage") == [(job["user_id"], job["id"])]

        # An attempt that stops makes its closing read due.
        assert await store.attempt_commands.transition(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="failed",
            detail="the agent exited",
        )
        assert "ledger" in await background.until_quiet(pool)
    finally:
        await owner.connection.close()
        await background.connection.close()


async def test_an_attempt_without_a_ledger_row_says_whether_it_ever_called_a_model(store, pool):
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="source checkout exceeds the retained import size limit",
    )
    rows = await store.ledger.ledger_rows_for_job(job_id=job["id"])
    assert [(row["id"], row["model_usage_reported"]) for row in rows] == [(None, False)]

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload) "
            "VALUES ($1, $2, 1, 'usage', '{\"input_tokens\": 1}'::jsonb)",
            job["id"],
            claim["attempt_id"],
        )
    rows = await store.ledger.ledger_rows_for_job(job_id=job["id"])
    assert rows[0]["model_usage_reported"] is True


async def test_the_earliest_scheduled_read_is_when_the_collector_wakes(store, pool):
    assert await store.ledger.next_ledger_refresh_due() is None
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    ledger_id = await _ledger_row(pool, claim["attempt_id"])
    soon = datetime.now(UTC) + timedelta(seconds=7)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET next_refresh_at = $2 WHERE id = $1", ledger_id, soon
        )
    due = await store.ledger.next_ledger_refresh_due()
    assert due is not None and abs((due - soon).total_seconds()) < 0.01


async def test_the_queue_and_tool_approvals_tell_their_owner(store, pool):
    job = await _create_job(store)
    owner = await _listen(OWNER)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ('apmsg_1', $1, $2, 'queued', 'and then the docs')",
                job["thread_id"],
                job["user_id"],
            )
            await conn.execute("UPDATE agent_pending_messages SET state = 'held'")
            await conn.execute("DELETE FROM agent_pending_messages")
        assert (
            _owner_hints(await owner.until_quiet(pool), "queue")
            == [(job["user_id"], job["thread_id"])] * 3
        )

        connection = await _connect(store, job["user_id"])
        invocation = await _invocation(store, job, connection)
        assert _owner_hints(await owner.until_quiet(pool), "mcp") == [(job["user_id"], job["id"])]
        assert await store.mcp.decide_mcp_invocation(
            invocation_id=int(invocation["id"]), approve=True, decided_by=job["user_id"]
        )
        assert _owner_hints(await owner.until_quiet(pool), "mcp") == [(job["user_id"], job["id"])]
    finally:
        await owner.connection.close()


async def test_publication_progress_tells_every_conversation_in_the_workspace_and_wakes_workers(
    store, pool
):
    job, _claim_row, sandbox = await _reserve_shared_chat(store)
    owner = await _listen(OWNER)
    background = await _listen(BACKGROUND)
    try:
        async with pool.acquire() as conn, conn.transaction():
            snapshot_id = await conn.fetchval(
                "INSERT INTO agent_sandbox_snapshots "
                "(sandbox_id, workspace_epoch, anchor_job_id, base_sha) "
                "VALUES ($1, 1, $2, $3) RETURNING id",
                sandbox["sandbox_id"],
                job["id"],
                "a" * 40,
            )
            await conn.execute(
                "INSERT INTO agent_snapshot_jobs (snapshot_id, job_id) VALUES ($1, $2)",
                snapshot_id,
                job["id"],
            )
        assert _owner_hints(await owner.until_quiet(pool), "publication") == [
            (job["user_id"], job["thread_id"])
        ]
        assert await background.until_quiet(pool) == ["snapshot"]

        async def state(new: str) -> list[str]:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_sandbox_snapshots SET state = $2 WHERE id = $1",
                    snapshot_id,
                    new,
                )
            return await background.until_quiet(pool)

        assert await state("capturing") == []
        # A deferred capture is retried on the coordinator's clock, not woken.
        assert await state("pending") == [], "a deferral must not wake the coordinator"
        assert await state("capturing") == []
        assert await state("captured") == ["publish"]
        assert await state("publishing") == []
        assert sorted(await state("published")) == ["publish", "snapshot"]
        # One per change of state: capturing, pending, capturing, captured,
        # publishing, published.
        assert len(_owner_hints(await owner.until_quiet(pool), "publication")) == 6
    finally:
        await owner.connection.close()
        await background.connection.close()


async def test_a_quiet_workspace_and_an_ended_turn_wake_the_snapshot_coordinator(store, pool):
    job, claim, sandbox = await _reserve_shared_chat(store)
    background = await _listen(BACKGROUND)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_sandboxes SET active_operation_count = 1 WHERE id = $1",
                sandbox["sandbox_id"],
            )
            assert await background.until_quiet(pool) == []
            await conn.execute(
                "UPDATE agent_sandboxes SET active_operation_count = 0 WHERE id = $1",
                sandbox["sandbox_id"],
            )
            assert await background.until_quiet(pool) == ["snapshot"]
            await conn.execute(
                "UPDATE agent_sandboxes SET snapshot_state = 'importing' WHERE id = $1",
                sandbox["sandbox_id"],
            )
            assert await background.until_quiet(pool) == ["publish"]
        assert await store.attempt_commands.transition(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="failed",
            detail="the agent exited",
        )
        assert "snapshot" in await background.until_quiet(pool)
    finally:
        await background.connection.close()


async def test_the_hints_and_wake_ups_go_with_a_downgrade_and_come_back(store, pool):
    config = _db_config()

    async def functions() -> set[str]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT proname FROM pg_proc WHERE proname IN ("
                "'agent_notify_owner', 'agent_notify_usage_changed', "
                "'agent_notify_queue_changed', 'agent_notify_mcp_changed', "
                "'agent_notify_publication_changed', 'agent_notify_background', "
                "'agent_notify_background_snapshot_state', 'agent_wake_background_from_trigger')"
            )
        return {row["proname"] for row in rows}

    assert len(await functions()) == 8
    try:
        downgrade_to(config, "0031_owner_change_notify")
        assert await functions() == set()
        job = await _create_job(store)
        background = await _listen(BACKGROUND)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled' WHERE id = $1", job["id"]
                )
            assert await background.until_quiet(pool) == []
        finally:
            await background.connection.close()
    finally:
        apply_migrations(config)
    assert len(await functions()) == 8
