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


async def test_a_call_reported_while_its_usage_is_read_is_read_again_after(store, pool):
    """A model call that finishes during a usage read schedules the next one.

    That read may have asked the gateway before the call finished, so its own
    completion keeps the request. Until then, another worker counts the row
    from the claim's expiry rather than looking again and again.
    """
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    ledger_id = await _ledger_row(pool, claim["attempt_id"])
    usage = {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}}

    async def report_a_call() -> None:
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload=usage,
        )

    async def scheduled() -> datetime | None:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT next_refresh_at FROM agent_attempt_ledger WHERE id = $1", ledger_id
            )

    await report_a_call()
    read = await store.ledger.claim_due_ledger_refresh()
    assert read is not None and await scheduled() is None

    await report_a_call()
    assert await scheduled() is not None, "a call during the read is asked for"
    due = await store.ledger.next_ledger_refresh_due()
    assert due is not None and due > datetime.now(UTC) + timedelta(seconds=30), (
        "while the read is claimed, nothing is due before its claim expires"
    )

    assert await store.ledger.complete_ledger_refresh(
        ledger_id=read["id"],
        refresh_generation=read["refresh_generation"],
        terminal=False,
        report={},
        error_code=None,
    )
    kept = await scheduled()
    assert kept is not None, "completing the read keeps the request made during it"
    assert await store.ledger.next_ledger_refresh_due() == kept

    # Without a call in between, a completed read leaves nothing scheduled.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET next_refresh_at = NOW() WHERE id = $1", ledger_id
        )
    read = await store.ledger.claim_due_ledger_refresh()
    assert read is not None
    assert await store.ledger.complete_ledger_refresh(
        ledger_id=read["id"],
        refresh_generation=read["refresh_generation"],
        terminal=False,
        report={},
        error_code=None,
    )
    assert await scheduled() is None


def _without_window_end(rows: list[dict]) -> list[dict]:
    """Rows as compared across reads: a running attempt's window ends at each read's NOW()."""
    return [{k: v for k, v in row.items() if k != "window_until"} for row in rows]


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
    ledger_id = await _ledger_row(pool, claim["attempt_id"])
    first = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert first is not None
    assert first["job"]["state"] == "running" and first["job"]["runtime"] == job["runtime"]
    assert _without_window_end(first["ledger_rows"]) == _without_window_end(
        await store.ledger.ledger_rows_for_job(job_id=job["id"])
    )
    assert first["conversation_rows"] == await store.ledger.ledger_rows_for_conversation(
        job_id=job["id"], user_id=job["user_id"]
    )
    assert first["inputs"] == await store.execution_queries.job_usage_inputs(job_id=job["id"])
    assert await store.ledger.job_usage_reads(job_id=job["id"], user_id="someone-else") is None

    # A usage event and a stored reading land while the read is under way,
    # after its first statement: neither is in it.
    real_inputs = ledger_repository.fetch_job_usage_inputs

    async def a_commit_lands_first(conn, job_id):
        async with pool.acquire() as other:
            await other.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload) "
                "VALUES ($1, $2, 1, 'usage', '{\"input_tokens\": 9}'::jsonb)",
                job["id"],
                claim["attempt_id"],
            )
            await other.execute(
                "UPDATE agent_attempt_ledger SET queried_at = NOW() WHERE id = $1", ledger_id
            )
        return await real_inputs(conn, job_id)

    monkeypatch.setattr(ledger_repository, "fetch_job_usage_inputs", a_commit_lands_first)
    during = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    monkeypatch.setattr(ledger_repository, "fetch_job_usage_inputs", real_inputs)
    assert during is not None
    assert during["inputs"]["events"] == [], "the event committed mid-read is not in it"
    assert during["ledger_rows"][0]["queried_at"] is None
    assert during["version"] >= first["version"]

    after = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert after is not None
    assert [event["event_type"] for event in after["inputs"]["events"]] == ["usage"]
    assert after["ledger_rows"][0]["queried_at"] is not None
    assert after["version"] > during["version"], "it saw a commit the other did not"
    again = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert again is not None and again["version"] >= after["version"]


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


async def _ended_shared_turn(store) -> dict:
    """A shared workspace whose one turn has ended, with its snapshot due."""
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
    return sandbox


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

    await _retry_now(pool, capture["snapshot_id"])
    assert await store.snapshot.pending_snapshot_base_import() is not None
    assert await store.snapshot.complete_snapshot_base_import(
        snapshot_id=capture["snapshot_id"], commit_sha=commit_sha
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
