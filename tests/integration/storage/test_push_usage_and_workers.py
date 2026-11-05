"""Usage, queue, approval and publication hints, and the workers' wake-ups, on Postgres.

Migration ``0032``. Owners' tabs are told, by job or conversation, when usage,
the message queue, a tool approval or publication progress changed; the
control plane's workers are told when there may be work for them. The
snapshot and publish workers are gone with the platform publisher (#1103); the
schema's triggers for them stay until a later release drops their tables, and
are asserted here while they stand. Negative answers are made exact with a sentinel:
Postgres delivers notifications in commit order, so if the sentinel sent
afterwards is the next one heard, nothing was sent before it.
"""

from __future__ import annotations

import asyncio
import json

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
from tests.support.usage import job_usage_inputs

from cloud_agent.workers.mcp_decision_signal import CHANNEL as DECISIONS
from cloud_agent.workers.owner_change_signal import CHANNEL as OWNER

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_SENTINEL = "sentinel"
#: The channel ``0032``'s triggers wake the control plane's workers on. The
#: snapshot and publish workers that listened are gone with the platform
#: publisher (#1103); the triggers stay until the release that drops them.
BACKGROUND = "agent_background_work"


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


async def test_a_model_calls_usage_is_pushed_to_its_owner_when_it_is_stored(store, pool):
    """The agent's usage report is what the owner's tabs show; each one tells them at once."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    owner = await _listen(OWNER)
    try:
        # An event without usage changes nobody's usage.
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="tool_result",
            payload={"content": "ok"},
        )
        assert _owner_hints(await owner.until_quiet(pool), "usage") == []

        # A model call's usage, nested (Claude) or as its own event (pi, Codex).
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}},
        )
        assert _owner_hints(await owner.until_quiet(pool), "usage") == [(job["user_id"], job["id"])]
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="usage",
            payload={"input_tokens": 4, "output_tokens": 2, "usage_id": "codex:t:6"},
        )
        assert _owner_hints(await owner.until_quiet(pool), "usage") == [(job["user_id"], job["id"])]
    finally:
        await owner.connection.close()


async def test_an_owners_usage_is_read_from_one_snapshot_whose_version_orders_it(
    store, pool, monkeypatch
):
    """A tab keeps the newer of a usage it read and one it was pushed, by version.

    The read is one ``REPEATABLE READ`` snapshot: a commit that lands while it
    reads is not half in it. Its version is greater than an earlier read's once
    anything was committed between them, and no read sees another owner's job.
    """
    from cloud_agent.storage.repositories import ledger as ledger_repository

    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    first = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert first is not None
    assert first["job"]["state"] == "running" and first["job"]["runtime"] == job["runtime"]
    assert set(first) == {"job", "inputs", "version"}
    assert first["inputs"] == await job_usage_inputs(pool, job_id=job["id"])
    assert await store.ledger.job_usage_reads(job_id=job["id"], user_id="someone-else") is None

    # A usage event lands while the read is under way, after its first
    # statement: it is not in it.
    real_inputs = ledger_repository.fetch_job_usage_inputs

    async def a_commit_lands_first(conn, job_id):
        async with pool.acquire() as other:
            await other.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload) "
                "VALUES ($1, $2, 1, 'usage', '{\"input_tokens\": 9}'::jsonb)",
                job["id"],
                claim["attempt_id"],
            )
        return await real_inputs(conn, job_id)

    monkeypatch.setattr(ledger_repository, "fetch_job_usage_inputs", a_commit_lands_first)
    during = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    monkeypatch.setattr(ledger_repository, "fetch_job_usage_inputs", real_inputs)
    assert during is not None
    assert during["inputs"]["events"] == [], "the event committed mid-read is not in it"
    assert during["version"] >= first["version"]

    after = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert after is not None
    assert [event["event_type"] for event in after["inputs"]["events"]] == ["usage"]
    assert after["version"] > during["version"], "it saw a commit the other did not"
    again = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert again is not None and again["version"] >= after["version"]


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


async def test_a_held_call_leaving_pending_wakes_the_relay_holding_it(store, pool):
    """Approved, denied or expired, the relay's request for that call is told.

    What follows an approval is the relay's own doing, and is not announced.
    """
    job = await _create_job(store)
    connection = await _connect(store, job["user_id"])
    approved = await _invocation(store, job, connection)
    denied = await _invocation(store, job, connection)
    expired = await _invocation(store, job, connection)
    decisions = await _listen(DECISIONS)
    try:
        assert await store.mcp.decide_mcp_invocation(
            invocation_id=int(approved["id"]), approve=True, decided_by=job["user_id"]
        )
        assert await store.mcp.decide_mcp_invocation(
            invocation_id=int(denied["id"]), approve=False, decided_by=job["user_id"]
        )
        assert await store.mcp.expire_mcp_invocation(invocation_id=int(expired["id"]))
        assert await decisions.until_quiet(pool) == [
            str(approved["id"]),
            str(denied["id"]),
            str(expired["id"]),
        ]

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_mcp_invocations SET status = 'dispatching' WHERE id = $1",
                int(approved["id"]),
            )
        assert await decisions.until_quiet(pool) == []
    finally:
        await decisions.connection.close()


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
                "'agent_notify_background_snapshot_state', 'agent_wake_background_from_trigger', "
                "'agent_notify_mcp_decided')"
            )
        return {row["proname"] for row in rows}

    assert len(await functions()) == 9
    # Seed with the current writer before restoring the historical schema:
    # newer thread columns need not exist at the revision under test.
    job = await _create_job(store)
    try:
        downgrade_to(config, "0031_owner_change_notify")
        assert await functions() == set()
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
    assert len(await functions()) == 9
