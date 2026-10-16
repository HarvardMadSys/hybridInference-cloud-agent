"""Postgres acceptance tests for immutable grant references and snapshot fencing."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_shared_chat,
)
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations
from tests.unit.test_ledger_snapshots import report_payload

from cloud_agent.ledger import summarize_conversation, summarize_job_usage
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.ledger import PostgresLedgerRepository
from cloud_agent.thread_stats import summarize_thread

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def db(monkeypatch):
    """Migrate a dedicated test database and clean only its Agent tables."""
    name = os.getenv("TEST_DB_NAME", "cloud_agent_ledger_test_db")
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    if worker != "master":
        name = f"{name}_{worker}"
    if "_test_" not in name:
        pytest.fail("Refusing to clean a database whose name does not contain _test_")
    config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    # Migration helpers normally respect this deployment variable; this
    # fixture must exclusively migrate the explicitly guarded test database.
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    apply_migrations(config)

    async def clean():
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)

    await clean()
    try:
        yield pool
    finally:
        await clean()
        await pool.close()


@pytest_asyncio.fixture
async def store(db):
    """Everything else this test needs, to set a claim up."""
    return Storage(db)


@pytest_asyncio.fixture
async def ledger(db):
    """The capability under test, on the repository that now owns it."""
    return PostgresLedgerRepository(db)


def _fence(job: dict, claim: dict, sandbox: dict) -> dict:
    return {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }


async def _residency(
    store: Storage,
    job: dict,
    claim: dict,
    sandbox: dict,
    *,
    grant_id: str = "grant-actual-resident",
    binding_id: str | None = "gateway-binding-one",
) -> dict:
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
        sandbox_id=sandbox["sandbox_id"],
    )
    assert residency is not None
    assert await store.grants.link_residency_grant(
        thread_id=job["thread_id"],
        generation=residency["generation"],
        grant_id=grant_id,
        binding_id=binding_id,
    )
    return residency


async def _record(store: Storage, job: dict, claim: dict, sandbox: dict, residency: dict):
    return await store.sandbox.record_shared_thread_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_id=sandbox["sandbox_id"],
        residency_generation=residency["generation"],
    )


async def _seed_attempt(
    db,
    store: Storage,
    *,
    job: dict | None = None,
    attempt_no: int = 1,
    grant_id: str | None = "grant-one",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> tuple[dict, int, int | None]:
    """Seed historical attempts to exercise refresh scheduling independently."""
    job = await _create_job(store) if job is None else job
    started_at = started_at or datetime.now(timezone.utc) - timedelta(seconds=10)
    async with db.acquire() as conn:
        attempt_id = await conn.fetchval(
            """
            INSERT INTO agent_attempts (
                job_id, thread_id, attempt_no, lease_owner, lease_generation,
                lease_expires_at, started_at, finished_at, status
            ) VALUES ($1, $6, $2, 'ledger-test', $2::integer, NOW() + INTERVAL '1 hour', $3, $4, $5)
            RETURNING id
            """,
            job["id"],
            attempt_no,
            started_at,
            finished_at,
            "running" if finished_at is None else "finished",
            job["thread_id"],
        )
        ledger_id = None
        if grant_id is not None:
            ledger_id = await conn.fetchval(
                """
                INSERT INTO agent_attempt_ledger (
                    attempt_id, grant_id, residency_generation, binding_id, window_since
                ) VALUES ($1, $2, 1, 'gateway-binding-one', $3) RETURNING id
                """,
                attempt_id,
                grant_id,
                started_at,
            )
    return job, attempt_id, ledger_id


async def _force_due(db, store: Storage, ledger_id: int) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET next_refresh_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            ledger_id,
        )


async def test_conversation_usage_is_owner_scoped_and_does_not_include_other_threads(
    db, store, ledger: PostgresLedgerRepository
):
    """One read includes sibling turns, keeps unstarted ones, and excludes forks."""
    # Real turns of one thread: the schema ties a job's parent, messages and
    # owner to its thread, so a conversation is built the way the API builds
    # one rather than by re-pointing rows. Another owner's job cannot be
    # placed in this thread at all any more; owner scoping is still asserted
    # on the read below.
    first, first_attempt, first_ledger = await _seed_attempt(db, store)
    second = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="second turn"
    )
    assert second is not None and second.get("error") is None
    _, second_attempt, second_ledger = await _seed_attempt(db, store, job=second)
    queued = await store.conversation_commands.create_follow_up(
        parent_job_id=second["id"], user_id="user-1", prompt="unstarted turn"
    )
    assert queued is not None and queued.get("error") is None
    other_thread, _, _ = await _seed_attempt(db, store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET snapshot = $2::jsonb, queried_at = NOW() "
            "WHERE id = ANY($1::bigint[])",
            [first_ledger, second_ledger],
            json.dumps(report_payload(grant_id="grant-one")),
        )
    rows = await ledger.ledger_rows_for_conversation(job_id=first["id"], user_id="user-1")
    assert {row["job_id"] for row in rows} == {first["id"], second["id"], queued["id"]}
    assert {row["attempt_id"] for row in rows} == {first_attempt, second_attempt, None}
    summary = summarize_conversation(rows)
    assert summary.turn_count == 3
    assert summary.totals.calls == 2
    assert summary.totals.spent_usd == "0.00024690"
    assert summary.incomplete is False
    assert other_thread["id"] not in {row["job_id"] for row in rows}
    assert (
        await ledger.ledger_rows_for_conversation(job_id=first["id"], user_id="another-user") == []
    )
    assert await ledger.ledger_rows_for_conversation(job_id="missing", user_id="user-1") == []


async def _complete(
    store: Storage,
    claim: dict,
    *,
    calls: int | None = 2,
    error: str | None = None,
    ledger: PostgresLedgerRepository,
) -> bool:
    return await ledger.complete_ledger_refresh(
        ledger_id=claim["id"],
        refresh_generation=claim["refresh_generation"],
        terminal=claim["terminal"],
        report=None if error else {"totals": {"calls": calls}},
        error_code=error,
    )


async def test_capture_actual_residency_grant_before_cli_and_preserve_rotations(
    db, store, ledger: PostgresLedgerRepository
):
    """Grant cleanup and a new residency cannot rewrite an executed attempt."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET grant_id = 'disposable-wrong-grant' WHERE id = $1",
            claim["attempt_id"],
        )
    residency = await _residency(store, job, claim, sandbox)
    assert await _record(store, job, claim, sandbox, residency)
    assert await _record(store, job, claim, sandbox, residency)
    rows = await ledger.ledger_rows_for_job(job_id=job["id"])
    assert len(rows) == 1
    original = rows[0]
    assert original["grant_id"] == "grant-actual-resident"
    assert original["binding_id"] == "gateway-binding-one"
    assert original["window_since"] == original["started_at"]
    assert original["snapshot"] is None and original["refreshing"] is False

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET grant_id = NULL WHERE id = $1", claim["attempt_id"]
        )
    rotated = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
        sandbox_id=sandbox["sandbox_id"],
    )
    assert rotated["previous_grant_id"] == original["grant_id"]
    assert rotated["grant_id"] is None and rotated["ledger_binding_id"] is None
    assert await store.grants.link_residency_grant(
        thread_id=job["thread_id"],
        generation=rotated["generation"],
        grant_id="grant-rotated",
        binding_id="gateway-binding-two",
    )
    assert await _record(store, job, claim, sandbox, rotated)
    rows = await ledger.ledger_rows_for_job(job_id=job["id"])
    assert len(rows) == 2
    assert rows[0]["id"] == original["id"]
    assert rows[0]["grant_id"] == original["grant_id"]
    assert rows[0]["residency_generation"] == residency["generation"]
    assert rows[0]["binding_id"] == "gateway-binding-one"
    assert rows[1]["grant_id"] == "grant-rotated"
    assert rows[1]["binding_id"] == "gateway-binding-two"
    assert rows[1]["window_since"] == original["window_since"]


async def test_hot_followup_captures_same_grant_for_its_own_attempt(
    store, ledger: PostgresLedgerRepository
):
    """A retained CLI gets a durable reference when its next prompt is admitted."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = _fence(job, claim, sandbox)
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="ledger-container", context_status="new"
    )
    residency = await _residency(store, job, claim, sandbox)
    assert await _record(store, job, claim, sandbox, residency)
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=residency["generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=residency["generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="ledger-native-session",
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
    followup = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="continue the retained turn"
    )
    following_claim = await _claim(
        store,
        worker_id="ledger-next-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
    )
    assert following_claim["id"] == followup["id"]
    following_sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=following_claim["attempt_id"],
        lease_generation=following_claim["lease_generation"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        container_name=sandbox["container_name"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        sandbox_image_digest=sandbox["sandbox_image_digest"],
        execution_profile=sandbox["execution_profile"],
    )
    next_fence = _fence(followup, following_claim, following_sandbox)
    assert await store.sandbox.mark_thread_sandbox_running(
        **next_fence, container_id="ledger-container", context_status="live"
    )
    assert (await ledger.ledger_rows_for_job(job_id=followup["id"]))[0]["id"] is None
    assert await store.sandbox.admit_thread_sandbox_turn(
        **next_fence, residency_generation=residency["generation"], job_id=followup["id"]
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **next_fence, residency_generation=residency["generation"], job_id=followup["id"]
    )
    rows = await ledger.ledger_rows_for_job(job_id=followup["id"])
    assert len(rows) == 1 and rows[0]["id"] is not None
    assert rows[0]["grant_id"] == "grant-actual-resident"
    first = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert first["id"] != rows[0]["id"]
    assert first["attempt_id"] == claim["attempt_id"]
    assert first["window_until"] <= rows[0]["window_since"]


@pytest.mark.parametrize("missing", ["binding", "grant"])
async def test_missing_historical_attribution_remains_untracked(
    db, store, missing, ledger: PostgresLedgerRepository
):
    """No grant or no binding must never become a false zero."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(
        store,
        job,
        claim,
        sandbox,
        binding_id=None if missing == "binding" else "gateway-binding-one",
    )
    if missing == "grant":
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships SET residency_grant_id = NULL "
                "WHERE thread_id = $1",
                job["thread_id"],
            )
    assert await _record(store, job, claim, sandbox, residency)
    assert await store.sandbox.admit_thread_sandbox_turn(
        **_fence(job, claim, sandbox),
        residency_generation=residency["generation"],
        job_id=job["id"],
    )
    rows = await ledger.ledger_rows_for_job(job_id=job["id"])
    assert len(rows) == 1
    assert rows[0]["attempt_id"] == claim["attempt_id"]
    assert rows[0]["id"] is None and rows[0]["snapshot"] is None
    assert rows[0]["refreshing"] is False
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 0
    assert await ledger.claim_due_ledger_refresh() is None


async def test_a_stale_residency_generation_is_refused_at_record(store):
    """The lease has one row now; a host echoing another generation is fenced out."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(store, job, claim, sandbox)
    stale = {**residency, "generation": residency["generation"] + 1}
    assert not await _record(store, job, claim, sandbox, stale)
    assert await _record(store, job, claim, sandbox, residency)


async def test_an_expired_residency_cannot_be_recorded_or_capture_a_ledger_row(
    db, store, ledger: PostgresLedgerRepository
):
    """The residency clock removes authority before the reaper changes its status."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(store, job, claim, sandbox)
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET residency_lease_expires_at = NOW() - INTERVAL '1 second'
            WHERE thread_id = $1
            """,
            job["thread_id"],
        )
        still_hot = await conn.fetchval(
            """
            SELECT residency_status = 'hot'
              AND residency_generation = $2
              AND residency_lease_expires_at <= NOW()
            FROM agent_thread_sandbox_memberships
            WHERE thread_id = $1
            """,
            job["thread_id"],
            residency["generation"],
        )
    assert still_hot, "the reaper must not be needed to make this lease invalid"

    assert not await _record(store, job, claim, sandbox, residency)
    rows = await ledger.ledger_rows_for_job(job_id=job["id"])
    assert len(rows) == 1
    assert rows[0]["attempt_id"] == claim["attempt_id"]
    assert rows[0]["id"] is None
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_attempt_ledger WHERE attempt_id = $1",
                claim["attempt_id"],
            )
            == 0
        )


async def test_residency_grant_link_rechecks_expiry_after_waiting_on_the_row(
    db,
    store: Storage,
):
    """A blocked link cannot attach a grant after the residency clock expires."""
    job, _claim_row, sandbox = await _reserve_shared_chat(store)
    generation = sandbox["residency_generation"]
    linking = None

    async with db.acquire() as setup:
        await setup.execute(
            "UPDATE agent_thread_sandbox_memberships "
            "SET residency_lease_expires_at = "
            "clock_timestamp() + INTERVAL '250 milliseconds' "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT thread_id FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1 FOR UPDATE",
            job["thread_id"],
        )
        linking = asyncio.create_task(
            store.grants.link_residency_grant(
                thread_id=job["thread_id"],
                generation=generation,
                grant_id="grant-too-late",
                binding_id="binding-too-late",
            )
        )
        waiting = False
        for _ in range(100):
            waiting = await blocker.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                blocker_pid,
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        if not waiting:
            linking.cancel()
            await asyncio.gather(linking, return_exceptions=True)
            pytest.fail("residency grant linking did not wait on the membership row lock")
        await asyncio.sleep(0.35)

    assert linking is not None
    assert not await asyncio.wait_for(linking, timeout=2)
    async with db.acquire() as conn:
        member = await conn.fetchrow(
            "SELECT residency_status, residency_grant_id, residency_binding_id, "
            "residency_lease_expires_at <= clock_timestamp() AS expired "
            "FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            job["thread_id"],
        )
    assert member["residency_status"] == "hot"
    assert member["expired"] is True
    assert member["residency_grant_id"] is None
    assert member["residency_binding_id"] is None


@pytest.mark.parametrize("write", ["startup", "admission"])
async def test_attribution_insert_failure_rolls_back_the_cli_admission(db, store, write):
    """A failed snapshot-reference write cannot acknowledge untracked execution."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(store, job, claim, sandbox)
    async with db.acquire() as conn:
        before_generation = await conn.fetchval(
            "SELECT residency_generation FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            job["thread_id"],
        )
        if write == "admission":
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships SET residency_generation = $2 "
                "WHERE thread_id = $1",
                job["thread_id"],
                residency["generation"],
            )
        await conn.execute(
            "ALTER TABLE agent_attempt_ledger ADD CONSTRAINT test_ledger_failure "
            "CHECK (FALSE) NOT VALID"
        )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            if write == "startup":
                await _record(store, job, claim, sandbox, residency)
            else:
                await store.sandbox.admit_thread_sandbox_turn(
                    **_fence(job, claim, sandbox),
                    residency_generation=residency["generation"],
                    job_id=job["id"],
                )
        async with db.acquire() as conn:
            member = await conn.fetchrow(
                "SELECT residency_generation, admitted_job_id "
                "FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                job["thread_id"],
            )
        assert member["admitted_job_id"] is None
        if write == "startup":
            assert member["residency_generation"] == before_generation
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "ALTER TABLE agent_attempt_ledger DROP CONSTRAINT test_ledger_failure"
            )


@pytest.mark.parametrize("expired", [False, True])
async def test_stale_attempt_cannot_capture_a_grant(
    db, store, expired, ledger: PostgresLedgerRepository
):
    """The execution fence, including expiry, protects both capture paths."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(store, job, claim, sandbox)
    if expired:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE id = $1",
                claim["attempt_id"],
            )
    else:
        claim["lease_generation"] += 1
    assert not await _record(store, job, claim, sandbox, residency)
    assert not await store.sandbox.admit_thread_sandbox_turn(
        **_fence(job, claim, sandbox),
        residency_generation=residency["generation"],
        job_id=job["id"],
    )
    assert (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]["id"] is None


@pytest.mark.parametrize("write", ["startup", "admission"])
async def test_capture_serializes_with_finish_without_inverting_attempt_membership_locks(
    db,
    store,
    write,
    ledger: PostgresLedgerRepository,
):
    """A finish owning the attempt can still close membership while capture waits."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(store, job, claim, sandbox)
    async with db.acquire() as conn:
        if write == "admission":
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships SET residency_generation = $2 "
                "WHERE thread_id = $1",
                job["thread_id"],
                residency["generation"],
            )
        async with conn.transaction():
            owner_pid = await conn.fetchval("SELECT pg_backend_pid()")
            await conn.fetchval(
                "SELECT id FROM agent_attempts WHERE id = $1 FOR UPDATE", claim["attempt_id"]
            )
            capture = asyncio.create_task(
                _record(store, job, claim, sandbox, residency)
                if write == "startup"
                else store.sandbox.admit_thread_sandbox_turn(
                    **_fence(job, claim, sandbox),
                    residency_generation=residency["generation"],
                    job_id=job["id"],
                )
            )
            try:
                for _ in range(100):
                    blocked = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                        owner_pid,
                    )
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
                assert blocked, "capture did not reach its attempt lock"
                # The old membership-first sequence holds this row while
                # its FK insert waits on the attempt, creating a deadlock.
                await asyncio.wait_for(
                    conn.execute(
                        "UPDATE agent_thread_sandbox_memberships "
                        "SET active_attempt_id = NULL WHERE thread_id = $1",
                        job["thread_id"],
                    ),
                    timeout=1,
                )
                await conn.execute(
                    "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                    "WHERE id = $1",
                    claim["attempt_id"],
                )
            except BaseException:
                capture.cancel()
                await asyncio.gather(capture, return_exceptions=True)
                raise
        assert await asyncio.wait_for(capture, timeout=1) is False
    assert (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]["id"] is None


async def test_all_retry_attempts_visible_and_windows_do_not_overlap(
    db, store, ledger: PostgresLedgerRepository
):
    """A delayed finish is clamped without extending a turn through idle gaps."""
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    job, first_id, _ = await _seed_attempt(
        db, store, started_at=start, finished_at=start + timedelta(seconds=120)
    )
    _, second_id, _ = await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=2,
        started_at=start + timedelta(seconds=60),
        finished_at=start + timedelta(seconds=90),
    )
    _, third_id, _ = await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=3,
        started_at=start + timedelta(seconds=180),
        grant_id=None,
    )
    # A different thread starting inside the window is not a boundary.
    await _seed_attempt(db, store, started_at=start + timedelta(seconds=20), grant_id=None)
    rows = await ledger.ledger_rows_for_job(job_id=job["id"])
    assert [row["attempt_id"] for row in rows] == [first_id, second_id, third_id]
    assert rows[0]["window_until"] == start + timedelta(seconds=60)
    assert rows[1]["window_until"] == start + timedelta(seconds=90)
    assert rows[2]["id"] is None and rows[2]["refreshing"] is False
    claimed = await ledger.claim_due_ledger_refresh()
    assert claimed["attempt_id"] == second_id  # Earlier completion has earlier due priority.
    assert claimed["window_until"] == rows[1]["window_until"]
    claimed = await ledger.claim_due_ledger_refresh()
    assert claimed["attempt_id"] == first_id
    assert claimed["window_until"] == rows[0]["window_until"]


async def test_refresh_claims_are_exclusive_and_expired_writes_are_fenced(
    db, store, ledger: PostgresLedgerRepository
):
    """Two workers cannot fetch one row, and an old response cannot replace a new one."""
    job, _, ledger_id = await _seed_attempt(db, store)
    assert await ledger.claim_due_ledger_refresh() is None
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 1
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 0
    claims = await asyncio.gather(
        ledger.claim_due_ledger_refresh(), ledger.claim_due_ledger_refresh()
    )
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    first = claimed[0]
    assert first["id"] == ledger_id and first["terminal"] is False
    assert (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]["refreshing"] is True
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 0
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger "
            "SET refresh_lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
            ledger_id,
        )
    assert not await _complete(store, first, ledger=ledger, calls=999)
    replacement = await ledger.claim_due_ledger_refresh()
    assert replacement["refresh_generation"] == first["refresh_generation"] + 1
    assert await _complete(store, replacement, ledger=ledger, calls=3)
    assert not await _complete(store, first, ledger=ledger, calls=999)
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["snapshot"]["totals"]["calls"] == 3
    assert row["refreshing"] is False


async def test_queue_refresh_rechecks_a_claim_expiry_after_a_plain_row_lock(
    db, store, ledger: PostgresLedgerRepository
):
    job, _, ledger_id = await _seed_attempt(db, store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger "
            "SET refresh_lease_expires_at = clock_timestamp() + INTERVAL '400 milliseconds', "
            "next_refresh_at = NULL WHERE id = $1",
            ledger_id,
        )

    queuing = None
    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT id FROM agent_attempt_ledger WHERE id = $1 FOR UPDATE",
            ledger_id,
        )
        queuing = asyncio.create_task(ledger.queue_job_ledger_refresh(job_id=job["id"]))
        waiting = False
        for _ in range(100):
            waiting = await blocker.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                blocker_pid,
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "refresh queueing did not wait on the ledger row lock"
        await asyncio.sleep(0.55)

    assert queuing is not None
    assert await asyncio.wait_for(queuing, timeout=2) == 1
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["next_refresh_at"] is not None
    # The lease is minted by Postgres, which may run on a VM with a different clock.
    async with db.acquire() as conn:
        database_now = await conn.fetchval("SELECT clock_timestamp()")
    assert row["refresh_lease_expires_at"] <= database_now


async def test_failure_preserves_cached_snapshot_and_throttles_manual_retry(
    db, store, ledger: PostgresLedgerRepository
):
    """Snapshot freshness and last-attempt time are intentionally different."""
    job, _, ledger_id = await _seed_attempt(db, store)
    await ledger.queue_job_ledger_refresh(job_id=job["id"])
    assert await _complete(store, await ledger.claim_due_ledger_refresh(), ledger=ledger, calls=7)
    cached = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    await _force_due(db, store, ledger_id)
    assert await _complete(
        store, await ledger.claim_due_ledger_refresh(), ledger=ledger, error="gateway_unavailable"
    )
    failed = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert failed["snapshot"] == cached["snapshot"]
    assert failed["queried_at"] == cached["queried_at"]
    assert failed["last_error_code"] == "gateway_unavailable"
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 1
    retry = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert retry["next_refresh_at"] >= retry["last_refresh_attempt_at"] + timedelta(seconds=5)
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 0
    assert await ledger.claim_due_ledger_refresh() is None
    await _force_due(db, store, ledger_id)
    assert await _complete(store, await ledger.claim_due_ledger_refresh(), ledger=ledger, calls=8)
    recovered = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert recovered["last_error_code"] is None
    assert recovered["snapshot"]["totals"]["calls"] == 8
    assert recovered["queried_at"] > cached["queried_at"]


async def test_terminal_refresh_retries_are_bounded_and_failures_consume_attempts(
    db, store, ledger: PostgresLedgerRepository
):
    """Terminal jobs refresh immediately, after 30 s, and at least 5 min after finish."""
    end = datetime.now(timezone.utc)
    job, attempt_id, ledger_id = await _seed_attempt(
        db, store, started_at=end - timedelta(minutes=10), finished_at=end
    )
    first = await ledger.claim_due_ledger_refresh()
    assert first["terminal"] is True and first["window_until"] == end
    assert await _complete(store, first, ledger=ledger, error="gateway_unavailable")
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["terminal_refresh_count"] == 1
    assert row["next_refresh_at"] == end + timedelta(seconds=30)
    assert await ledger.claim_due_ledger_refresh() is None

    # Advance the fixture's terminal age rather than sleeping; forcing only
    # next_refresh_at must no longer consume a reconciliation ahead of time.
    async with db.acquire() as conn:
        end = await conn.fetchval(
            "UPDATE agent_attempts SET finished_at = NOW() - INTERVAL '31 seconds' "
            "WHERE id = $1 RETURNING finished_at",
            attempt_id,
        )
    await _force_due(db, store, ledger_id)
    assert await _complete(store, await ledger.claim_due_ledger_refresh(), ledger=ledger, calls=2)
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["terminal_refresh_count"] == 2
    assert row["next_refresh_at"] >= end + timedelta(seconds=300)
    assert await ledger.claim_due_ledger_refresh() is None

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET finished_at = NOW() - INTERVAL '301 seconds' WHERE id = $1",
            attempt_id,
        )
    await _force_due(db, store, ledger_id)
    assert await _complete(
        store, await ledger.claim_due_ledger_refresh(), ledger=ledger, error="gateway_unavailable"
    )
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["terminal_refresh_count"] == 3
    assert row["next_refresh_at"] is None
    assert row["snapshot"]["totals"]["calls"] == 2
    assert await ledger.claim_due_ledger_refresh() is None
    # Bounded automatic retries do not disable an owner's explicit later retry.
    assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 1
    await _force_due(db, store, ledger_id)
    assert await _complete(store, await ledger.claim_due_ledger_refresh(), ledger=ledger, calls=5)
    assert (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]["next_refresh_at"] is None


async def test_early_manual_reads_preserve_both_terminal_reconciliations(
    db, store, ledger: PostgresLedgerRepository
):
    """Refresh clicks can replace cached data without spending the later stages."""
    end = datetime.now(timezone.utc)
    job, attempt_id, ledger_id = await _seed_attempt(
        db, store, started_at=end - timedelta(minutes=10), finished_at=end
    )
    assert await _complete(store, await ledger.claim_due_ledger_refresh(), ledger=ledger, calls=1)

    for stage, delay in ((1, 30), (2, 300)):
        if stage == 2:
            # The +30-second reconciliation is now due; its failure still
            # consumes this stage and schedules the five-minute read.
            async with db.acquire() as conn:
                end = await conn.fetchval(
                    "UPDATE agent_attempts SET finished_at = NOW() - INTERVAL '31 seconds' "
                    "WHERE id = $1 RETURNING finished_at",
                    attempt_id,
                )
            await _force_due(db, store, ledger_id)
            assert await _complete(
                store,
                await ledger.claim_due_ledger_refresh(),
                ledger=ledger,
                error="usage_unavailable",
            )
        for click in range(3):
            # Bypass only the click throttle. The reconciliation's actual
            # time boundary remains in the future for every manual read.
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_attempt_ledger "
                    "SET last_refresh_attempt_at = NOW() - INTERVAL '6 seconds' WHERE id = $1",
                    ledger_id,
                )
            assert await ledger.queue_job_ledger_refresh(job_id=job["id"]) == 1
            claim = await ledger.claim_due_ledger_refresh()
            assert claim is not None
            assert await _complete(
                store,
                claim,
                ledger=ledger,
                calls=click + 2,
                error="usage_unavailable" if click == 1 else None,
            )
            row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
            assert row["terminal_refresh_count"] == stage
            assert row["next_refresh_at"] == end + timedelta(seconds=delay)
            assert row["last_refresh_attempt_at"] < row["next_refresh_at"]
            assert await ledger.claim_due_ledger_refresh() is None


@pytest.mark.parametrize("failure", [False, True])
async def test_slow_early_read_cannot_consume_the_five_minute_reconciliation(
    db, store, failure, ledger: PostgresLedgerRepository
):
    """A query started at +299 seconds stays early even if it finishes after +300."""
    end = datetime.now(timezone.utc) - timedelta(seconds=301)
    job, _, ledger_id = await _seed_attempt(
        db, store, started_at=end - timedelta(minutes=10), finished_at=end
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET terminal_refresh_count = 2 WHERE id = $1",
            ledger_id,
        )
    await _force_due(db, store, ledger_id)
    claim = await ledger.claim_due_ledger_refresh()
    async with db.acquire() as conn:
        # Model the persisted start of an HTTP query whose response is only
        # now arriving. Its lease/generation remain live and unchanged.
        await conn.execute(
            "UPDATE agent_attempt_ledger SET last_refresh_attempt_at = $2 WHERE id = $1",
            ledger_id,
            end + timedelta(seconds=299),
        )
    assert await _complete(
        store, claim, ledger=ledger, calls=3, error="usage_unavailable" if failure else None
    )
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["terminal_refresh_count"] == 2
    assert row["next_refresh_at"] >= end + timedelta(seconds=300)
    final = await ledger.claim_due_ledger_refresh()
    assert final is not None and final["refresh_generation"] > claim["refresh_generation"]
    assert await _complete(store, final, ledger=ledger, error="usage_unavailable")
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["terminal_refresh_count"] == 3 and row["next_refresh_at"] is None
    assert await ledger.claim_due_ledger_refresh() is None


async def test_running_query_finishing_after_attempt_still_gets_closed_window_refresh(
    db, store, ledger: PostgresLedgerRepository
):
    """An in-flight running snapshot cannot satisfy the first terminal refresh."""
    job, attempt_id, _ = await _seed_attempt(db, store)
    await ledger.queue_job_ledger_refresh(job_id=job["id"])
    running = await ledger.claim_due_ledger_refresh()
    async with db.acquire() as conn:
        end = await conn.fetchval(
            "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
            "WHERE id = $1 RETURNING finished_at",
            attempt_id,
        )
    assert await _complete(store, running, ledger=ledger, calls=1)
    row = (await ledger.ledger_rows_for_job(job_id=job["id"]))[0]
    assert row["terminal_refresh_count"] == 0 and row["refreshing"] is True
    terminal = await ledger.claim_due_ledger_refresh()
    assert terminal["terminal"] is True
    assert terminal["window_until"] == end > running["window_until"]


async def test_job_runtime_usage_includes_retries_but_excludes_other_turns(db, store):
    """Comparison data retains usage identities and output detection without transcript text."""
    job, first, _ = await _seed_attempt(db, store, grant_id=None)
    _, retry, _ = await _seed_attempt(db, store, job=job, attempt_no=2, grant_id=None)
    # A real second turn of the same thread: the schema ties a job's messages
    # and parent to its thread, so a job cannot simply be re-pointed.
    sibling = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="a sibling turn"
    )
    assert sibling is not None and sibling.get("error") is None
    _, sibling_attempt, _ = await _seed_attempt(db, store, job=sibling, grant_id=None)
    async with db.acquire() as conn:
        for event_job, attempt_id, tokens, text in [
            (job["id"], first, 10, " \n\t"),
            (job["id"], retry, 20, "non-empty output"),
            (sibling["id"], sibling_attempt, 1000, "unrelated turn"),
        ]:
            await conn.execute(
                """
                INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)
                VALUES ($1, $2, 1, 'message', $3::jsonb)
                """,
                event_job,
                attempt_id,
                json.dumps(
                    {
                        "text": text,
                        "input": {"large": "input"},
                        "content": "bulky transcript",
                        "usage_id": "same-id-distinct-attempt",
                        "usage_scope": "message",
                        "usage": {"input_tokens": tokens, "output_tokens": 0},
                    }
                ),
            )
    inputs = await store.execution_queries.job_usage_inputs(job_id=job["id"])
    assert inputs["job_ids"] == [job["id"]]
    assert len(inputs["attempts"]) == 2
    assert [event["attempt_id"] for event in inputs["events"]] == [first, retry]
    assert [event["payload"]["has_text"] for event in inputs["events"]] == [False, True]
    assert all(
        not {"text", "input", "content"}.intersection(event["payload"])
        for event in inputs["events"]
    )
    summary = summarize_thread(**inputs)
    assert summary.input_tokens == 30
    assert summary.output_tokens is None


async def test_usage_reads_fetch_only_what_their_summaries_read(db, store):
    """A turn's tool output stays in Postgres, and its usage adds up as before.

    A job's usage read, which every open tab is pushed again as each model
    call is counted, and a thread's stats both took every event's payload
    but its text, input and content. A tool result also carries what the
    tool returned, such as a whole edited file or a command's output, so a
    long turn's read moved megabytes to add up a few token counts.
    """
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    job, first, _ = await _seed_attempt(
        db, store, grant_id=None, started_at=base, finished_at=base + timedelta(seconds=60)
    )
    _, retry, _ = await _seed_attempt(
        db, store, job=job, attempt_no=2, grant_id=None, started_at=base + timedelta(seconds=90)
    )
    output = "x = 1\n" * 5000
    # Usage in each place a runtime puts it, tool calls with large results,
    # and a retry whose last event totals its own usage.
    events = [
        (
            first,
            "message",
            {
                "text": "Looking.",
                "usage_id": "msg_1",
                "usage": {"input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 50},
            },
        ),
        (
            first,
            "tool_use",
            {
                "name": "Edit",
                "id": "call-1",
                "input": {"file_path": "a.py"},
                "usage_id": "msg_1",
                "usage": {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 50},
            },
        ),
        (
            first,
            "tool_result",
            {
                "tool_use_id": "call-1",
                "is_error": False,
                "content": "Updated.",
                "structured_output": {"filePath": "a.py", "originalFile": output},
            },
        ),
        (first, "tool_use", {"name": "Bash", "id": "call-2", "input": {"command": "pytest"}}),
        (
            first,
            "tool_result",
            {
                "tool_use_id": "call-2",
                "is_error": True,
                "content": "1 failed",
                "structured_output": {"stdout": output},
            },
        ),
        (first, "usage", {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 1}),
        (first, "thinking", {"text": "Nearly there.", "signature": output}),
        (
            first,
            "message",
            {
                "text": "Done.",
                "usage_id": "msg_2",
                "usage": {"input_tokens": 5, "output_tokens": 9},
            },
        ),
        (
            retry,
            "message",
            {
                "text": "Again.",
                "usage_id": "msg_1",
                "usage": {"input_tokens": 20, "output_tokens": 4},
            },
        ),
        (retry, "tool_use", {"name": "Read", "id": 7}),
        (
            retry,
            "tool_result",
            {"tool_use_id": 7, "is_error": 1, "structured_output": {"file": output}},
        ),
        (
            retry,
            "lifecycle",
            {
                "phase": "result",
                "usage_scope": "attempt",
                "usage": {"input_tokens": 30, "output_tokens": 8},
            },
        ),
    ]
    async with db.acquire() as conn:
        for n, (attempt_id, event_type, payload) in enumerate(events):
            await conn.execute(
                """
                INSERT INTO agent_job_events
                    (job_id, attempt_id, seq, event_type, payload, created_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                """,
                job["id"],
                attempt_id,
                n + 1,
                event_type,
                json.dumps(payload),
                base + timedelta(seconds=5 * n + 1),
            )
        full = [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in await conn.fetch(
                "SELECT job_id, attempt_id, seq, event_type, created_at, payload "
                "FROM agent_job_events WHERE job_id = $1 ORDER BY attempt_id, seq",
                job["id"],
            )
        ]
    reads = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    thread = await store.execution_queries.thread_stats_inputs(
        job_id=job["id"], user_id=job["user_id"]
    )
    assert reads is not None and thread is not None

    for inputs in (reads["inputs"], thread):
        assert max(len(json.dumps(event["payload"])) for event in inputs["events"]) < 256
        summary = summarize_thread(
            job_ids=inputs["job_ids"], attempts=inputs["attempts"], events=inputs["events"]
        )
        assert summary == summarize_thread(
            job_ids=inputs["job_ids"], attempts=inputs["attempts"], events=full
        )
    usage = summarize_job_usage(
        ledger_rows=reads["ledger_rows"],
        runtime_inputs=reads["inputs"],
        runtime="claude-code",
        terminal=False,
    )
    assert usage == summarize_job_usage(
        ledger_rows=reads["ledger_rows"],
        runtime_inputs={**reads["inputs"], "events": full},
        runtime="claude-code",
        terminal=False,
    )
    assert (usage.runtime.input_tokens, usage.runtime.output_tokens) == (142, 60)


async def test_deleting_attempt_cascades_its_snapshot_but_not_other_attempts(
    db, store, ledger: PostgresLedgerRepository
):
    """New references follow the existing Agent database retention boundary."""
    job, first, _ = await _seed_attempt(db, store)
    _, second, _ = await _seed_attempt(db, store, job=job, attempt_no=2)
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM agent_attempts WHERE id = $1", first)
    rows = await ledger.ledger_rows_for_job(job_id=job["id"])
    assert len(rows) == 1 and rows[0]["attempt_id"] == second
