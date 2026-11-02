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
import hashlib
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

from cloud_agent.workers.background_wake import CHANNEL as BACKGROUND
from cloud_agent.workers.mcp_decision_signal import CHANNEL as DECISIONS
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


async def _ended_shared_turn(store, *, requested: bool = True) -> dict:
    """A shared workspace whose one turn has ended and, if ``requested``, whose owner
    asked for its publication, so its snapshot is due."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-deferred", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-deferred",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    if requested:
        requested_snapshot = await store.snapshot.request_snapshot_publication(
            thread_id=job["thread_id"], user_id=job["user_id"]
        )
        assert requested_snapshot.get("requested") is True, requested_snapshot
    return {**sandbox, "job": job}


async def test_an_ended_turn_waits_and_the_owners_request_wakes_the_coordinator(store, pool):
    """A turn boundary leaves nothing due; the owner's request is what wakes capture."""
    sandbox = await _ended_shared_turn(store, requested=False)
    assert await store.snapshot.claim_due_sandbox_snapshot() is None, "nothing was requested"
    background = await _listen(BACKGROUND)
    try:
        job = sandbox["job"]
        requested = await store.snapshot.request_snapshot_publication(
            thread_id=job["thread_id"], user_id=job["user_id"]
        )
        assert requested.get("requested") is True, requested
        assert "snapshot" in await background.until_quiet(pool)
        capture = await store.snapshot.claim_due_sandbox_snapshot()
        assert capture is not None and capture["snapshot_id"] == requested["snapshot_id"]
    finally:
        await background.connection.close()


async def _retry_now(pool, snapshot_id: int) -> None:
    """Let a scheduled retry come due."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET retry_at = NOW() WHERE id = $1", snapshot_id
        )


async def test_a_deferred_or_stalled_capture_leaves_the_coordinator_asleep(store, pool):
    """The real claim, deferral and stall reaper: a capture handed back wakes nothing.

    Both return the workspace from ``snapshotting`` to ``pending``. A deferred
    capture waits out its retry time, which is when the coordinator looks
    again; a wake-up would claim it again at once, and a busy host would be
    asked in a loop.
    """
    sandbox = await _ended_shared_turn(store)
    background = await _listen(BACKGROUND)
    try:
        capture = await store.snapshot.claim_due_sandbox_snapshot()
        assert capture is not None and capture["sandbox_id"] == sandbox["sandbox_id"]
        assert await background.until_quiet(pool) == []

        assert await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            detail="a turn entered the workspace",
        )
        assert await background.until_quiet(pool) == [], "a deferral must not wake it"
        assert await store.snapshot.claim_due_sandbox_snapshot() is None, "not before its retry"
        due = await store.snapshot.next_snapshot_retry_due()
        assert due is not None
        assert 3 < (due - await pool.fetchval("SELECT clock_timestamp()")).total_seconds() <= 5, (
            "its retry is when it is due"
        )

        await _retry_now(pool, capture["snapshot_id"])
        assert await store.snapshot.next_snapshot_retry_due() is None
        capture = await store.snapshot.claim_due_sandbox_snapshot()
        assert capture is not None, "at its retry time the coordinator finds it again"
        assert await store.snapshot.reap_stalled_sandbox_snapshot_captures(stall_seconds=0) == [
            capture["snapshot_id"]
        ]
        assert await background.until_quiet(pool) == [], "nor must a reaped stall"
        assert await store.snapshot.claim_due_sandbox_snapshot() is not None, (
            "a stall the reaper returned is due at once"
        )
    finally:
        await background.connection.close()


async def test_a_failed_base_import_waits_out_its_retry(store, pool):
    """Not handed out again before its retry time, which is when the publisher wakes."""
    await _ended_shared_turn(store)
    capture = await store.snapshot.claim_due_sandbox_snapshot()
    assert capture is not None
    patch = "diff --git a/answer.txt b/answer.txt\nnew file mode 100644\n"
    assert (
        await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=capture["snapshot_id"],
            sandbox_id=capture["sandbox_id"],
            sandbox_generation=capture["generation"],
            container_id=capture["container_id"],
            workspace_epoch=capture["workspace_epoch"],
            base_sha=capture["base_sha"],
            patch=patch,
            content_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        )
        == "captured"
    )
    publication = await store.snapshot.claim_for_publish()
    assert publication is not None
    commit_sha = "b" * 40
    assert await store.snapshot.record_snapshot_publish(
        snapshot_id=capture["snapshot_id"],
        expected_publish_attempt_count=publication["publish_attempt_count"],
        pr_url="https://example.test/pull/1",
        commit_sha=commit_sha,
    )
    assert (await store.snapshot.pending_snapshot_base_import())["snapshot_id"] == capture[
        "snapshot_id"
    ]
    assert await store.snapshot.next_base_import_retry_due() is None

    await store.snapshot.record_snapshot_base_import_failure(
        snapshot_id=capture["snapshot_id"], detail="the sandbox manager is unavailable"
    )
    assert await store.snapshot.pending_snapshot_base_import() is None, "not before its retry"
    due = await store.snapshot.next_base_import_retry_due()
    assert due is not None
    assert 13 < (due - await pool.fetchval("SELECT clock_timestamp()")).total_seconds() <= 15
    # A credential that hit GitHub's rate limit waits as long as GitHub asked.
    await store.snapshot.record_snapshot_base_import_failure(
        snapshot_id=capture["snapshot_id"], detail="GitHub's rate limit", delay_s=600.0
    )
    due = await store.snapshot.next_base_import_retry_due()
    assert due is not None
    assert 595 < (due - await pool.fetchval("SELECT clock_timestamp()")).total_seconds() <= 600

    await _retry_now(pool, capture["snapshot_id"])
    pending = await store.snapshot.pending_snapshot_base_import()
    assert pending is not None
    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=capture["snapshot_id"],
        commit_sha=commit_sha,
        generation=pending["generation"],
        container_id=pending["container_id"],
    )
    async with pool.acquire() as conn:
        retry_at = await conn.fetchval(
            "SELECT retry_at FROM agent_sandbox_snapshots WHERE id = $1", capture["snapshot_id"]
        )
    assert retry_at is None


async def test_a_workspace_back_with_a_snapshot_pending_wakes_the_coordinator(store, pool):
    sandbox = await _ended_shared_turn(store)
    background = await _listen(BACKGROUND)
    try:
        async with pool.acquire() as conn:

            async def state(new: str) -> list[str]:
                await conn.execute(
                    "UPDATE agent_sandboxes SET state = $2 WHERE id = $1",
                    sandbox["sandbox_id"],
                    new,
                )
                return await background.until_quiet(pool)

            assert await state("stopping") == []
            assert await state("stopped") == ["snapshot"]
            assert await state("stopped") == [], "only a change of state"
    finally:
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
