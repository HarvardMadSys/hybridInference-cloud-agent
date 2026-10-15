"""The host pool against a real Postgres: capacity, capability, and the reaper.

Everything here needs the database to mean anything. A slot counter that never
goes negative is a statement about two transactions racing; an atomic
single-use token is a statement about an UPDATE's predicate; "capabilities
superset" is a jsonb containment operator. None of those can be tested against
a fake without testing the fake.

The fixture applies Alembic — the host pool's tables are in
`0001_schema_baseline` — exactly as a deploy does, so a migration that
produces a nearly-right schema fails here rather than at cutover.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.application import residency
from cloud_agent.host_pool import (
    HOST_DISABLED,
    HOST_DRAINING,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    hash_secret,
)
from cloud_agent.storage._sql.hosts import release_host_slot
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_S3_URI

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


@pytest_asyncio.fixture
async def db():
    """A clean store over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=10)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(db_config)

    async def _reset() -> None:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)

    await _reset()
    try:
        yield pool
    finally:
        await _reset()
        await pool.close()


@pytest_asyncio.fixture
async def store(db):
    """Everything else these tests need, to claim and settle work."""
    return Storage(db)


@pytest_asyncio.fixture
async def hosts(db):
    """The capability under test, on the repository that now owns it."""
    return PostgresHostPoolRepository(db)


async def _create_job(store: Storage, **overrides) -> dict:
    params = {
        "user_id": "u1",
        "repo": "acme/widgets",
        "task_prompt": "do the thing",
        "runtime": "claude-code",
        "model": "glm-5.1",
    }
    params.update(overrides)
    return await store.conversation_commands.create_job(**params)


async def _settled(db, store: Storage, job_id: str) -> None:
    """Stand in for the runner's settlement: a success carries its native ordinal.

    The runner settles the shared workspace before it reports success and the
    settlement assigns ``native_turn_no``; the schema refuses a succeeded row
    without one. Tests that skip the sandbox protocol and finish a job
    directly prepare that fact here, instead of the store guessing it.
    """
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET native_turn_no = turn_no "
            "WHERE id = $1 AND native_turn_no IS NULL",
            job_id,
        )


async def _expire_residency(db, *, thread_id: str) -> None:
    """Move a residency past its clock lease without waiting for the reaper."""
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships "
            "SET residency_lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE thread_id = $1",
            thread_id,
        )


def _archive_settings(**changes) -> dict:
    values = {
        "enabled": True,
        "region": "us-east-1",
        "kms_key_arn": (
            "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
        ),
        "expected_bucket_owner": "123456789012",
        "max_archive_bytes": 2 * 1024**3,
        "multipart_threshold_bytes": 8 * 1024**2,
        "multipart_part_bytes": 8 * 1024**2,
    }
    values.update(changes)
    return values


async def _enrol(store: Storage, host: str, hosts: PostgresHostPoolRepository, **overrides) -> dict:
    """Register a host and put it online, the way a real boot does."""
    row = await hosts.register_host(
        host=host, credential_hash=hash_secret(f"cred-{host}"), **overrides
    )
    assert row["status"] == HOST_ENROLLING
    return await hosts.record_host_heartbeat(host=host)


def _kata_attestation(**overrides) -> dict:
    stamp = {
        "sandbox_runtime": "io.containerd.kata.v2",
        "sandbox_image_digest": "sandbox@sha256:claim",
        "runner_capabilities": [
            "shared-sandbox-v1",
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability("test-deployment"),
        ],
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
        "host_kernel": "6.8.0-host",
        "guest_kernel": "6.1.0-kata",
    }
    stamp.update(overrides)
    return stamp


def _shared_attestation(**overrides) -> dict:
    """Exact image plus process-local capability proof for shared-live claims."""
    stamp = {
        "sandbox_runtime": "",
        "sandbox_image_digest": "sandbox@sha256:shared",
        "runner_capabilities": [
            "shared-sandbox-v1",
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability("test-deployment"),
        ],
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
    }
    stamp.update(overrides)
    return stamp


_DEFAULT_CLAIM_VALUE = object()


async def _claim(
    store: Storage,
    *,
    worker_id: str,
    lease_ttl_seconds: float,
    host: str = "",
    host_credential_hash: str | object = _DEFAULT_CLAIM_VALUE,
    claim_attestation: dict | object = _DEFAULT_CLAIM_VALUE,
    expected_deployment_sha: str | None = None,
) -> dict | None:
    """Claim as an enrolled, exactly attested host unless a test overrides it."""
    credential = (
        hash_secret(f"cred-{host}")
        if host_credential_hash is _DEFAULT_CLAIM_VALUE and host
        else ("" if host_credential_hash is _DEFAULT_CLAIM_VALUE else host_credential_hash)
    )
    attestation = (
        _shared_attestation() if claim_attestation is _DEFAULT_CLAIM_VALUE else claim_attestation
    )
    assert isinstance(credential, str)
    assert isinstance(attestation, dict)
    return await store.attempt_commands.claim_job(
        worker_id=worker_id,
        lease_ttl_seconds=lease_ttl_seconds,
        host=host,
        host_credential_hash=credential,
        claim_attestation=attestation,
        expected_deployment_sha=expected_deployment_sha,
    )


# ── Enrollment tokens ──────────────────────────────────────────────────


async def test_an_enrollment_token_is_spendable_exactly_once(
    store, hosts: PostgresHostPoolRepository
):
    """Same name both times: the second refusal must be replay, not the binding."""
    await hosts.issue_enroll_token(token_hash=hash_secret("t"), host="runner-a", ttl_seconds=900)

    first = await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")
    second = await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")

    assert (first, second) == (True, False)


async def test_two_machines_racing_one_token_produce_one_winner(
    store, hosts: PostgresHostPoolRepository
):
    """The single-use property is the UPDATE's predicate, not a read-then-write.

    A token pasted into two consoles at once is exactly how a fleet gets
    enrolled from one credential, and a check-then-set would let both through.
    Every racer claims the name the token names, so what is under test is the
    predicate on ``consumed_at`` rather than the one on ``host``.
    """
    await hosts.issue_enroll_token(token_hash=hash_secret("t"), host="runner-a", ttl_seconds=900)

    results = await asyncio.gather(
        *(
            hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")
            for _ in range(8)
        )
    )

    assert sum(results) == 1


async def test_an_expired_token_cannot_be_spent(store, hosts: PostgresHostPoolRepository):
    await hosts.issue_enroll_token(token_hash=hash_secret("t"), host="runner-a", ttl_seconds=-1)

    assert not await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")


async def test_enrollment_token_expiry_is_rechecked_after_a_plain_row_lock(
    db, hosts: PostgresHostPoolRepository
):
    token_hash = hash_secret("blocked-token")
    await hosts.issue_enroll_token(
        token_hash=token_hash,
        host="runner-a",
        ttl_seconds=0.4,
    )
    consuming = None
    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT token_hash FROM agent_host_enroll_tokens WHERE token_hash = $1 FOR UPDATE",
            token_hash,
        )
        consuming = asyncio.create_task(
            hosts.consume_enroll_token(token_hash=token_hash, host="runner-a")
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
        assert waiting, "token consumption did not wait on the token row lock"
        await asyncio.sleep(0.55)

    assert consuming is not None
    assert not await asyncio.wait_for(consuming, timeout=2)
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT consumed_at, expires_at <= clock_timestamp() AS expired "
            "FROM agent_host_enroll_tokens WHERE token_hash = $1",
            token_hash,
        )
    assert row["consumed_at"] is None
    assert row["expired"] is True


async def test_a_token_is_spendable_only_under_the_name_it_names(
    store, hosts: PostgresHostPoolRepository
):
    """The name is the token's, not the registering machine's.

    ``register_host`` upserts, so spending a token under a live host's name
    revokes that host's credential and resets its row — a typo during a setup
    took a working machine out of the pool. The name therefore comes from the
    token an admin issued.
    """
    await hosts.issue_enroll_token(token_hash=hash_secret("t"), host="runner-b", ttl_seconds=900)

    assert not await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")


async def test_a_mismatched_name_does_not_spend_the_token(store, hosts: PostgresHostPoolRepository):
    """A typo is retried, not re-issued: the name is a predicate, not a check."""
    await hosts.issue_enroll_token(token_hash=hash_secret("t"), host="runner-b", ttl_seconds=900)
    await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")

    assert await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-b")


async def test_a_typo_cannot_revoke_a_live_hosts_credential(
    store, hosts: PostgresHostPoolRepository
):
    """The eviction the binding removes, end to end against the real schema."""
    await _enrol(store, "runner-a", slots_total=4, hosts=hosts)
    await hosts.issue_enroll_token(token_hash=hash_secret("t"), host="runner-b", ttl_seconds=900)

    spent = await hosts.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")

    assert spent is False
    survivor = await hosts.get_host(host="runner-a")
    assert survivor["status"] == HOST_ONLINE
    assert survivor["enrolled"] is True
    assert await hosts.authenticate_host(
        host="runner-a", credential_hash=hash_secret("cred-runner-a")
    )


# ── Registration, authentication, heartbeat ────────────────────────────


async def test_registration_stores_what_the_host_declared(store, hosts: PostgresHostPoolRepository):
    await hosts.register_host(
        host="runner-a",
        credential_hash=hash_secret("cred"),
        capabilities=["runc", "kata", "kata"],
        slots_total=3,
        sandbox_manager_url="http://sandbox-manager-a:8092",
        runner_version="1.2.3",
        labels={"region": "cambridge"},
    )

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_ENROLLING
    assert row["capabilities"] == ["kata", "runc"]
    assert row["slots_total"] == 3
    assert row["sandbox_manager_url"] == "http://sandbox-manager-a:8092"
    assert row["labels"] == {"region": "cambridge"}
    assert row["enrolled"] is True


async def test_the_credential_hash_never_leaves_the_store(store, hosts: PostgresHostPoolRepository):
    """Reading it back out would make the row as good as the credential."""
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("cred"))

    assert "credential_hash" not in (await hosts.get_host(host="runner-a"))


async def test_only_the_current_credential_authenticates(store, hosts: PostgresHostPoolRepository):
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("first"))
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("second"))

    assert (
        await hosts.authenticate_host(host="runner-a", credential_hash=hash_secret("first")) is None
    )
    assert await hosts.authenticate_host(host="runner-a", credential_hash=hash_secret("second"))


async def test_a_disabled_host_authenticates_nothing(store, hosts: PostgresHostPoolRepository):
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("cred"))
    await hosts.remove_host(host="runner-a")

    assert (
        await hosts.authenticate_host(host="runner-a", credential_hash=hash_secret("cred")) is None
    )


async def test_the_first_heartbeat_promotes_and_records(store, hosts: PostgresHostPoolRepository):
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("cred"))

    row = await hosts.record_host_heartbeat(host="runner-a", runner_version="9.9")

    assert row["status"] == HOST_ONLINE
    assert row["runner_version"] == "9.9"
    assert row["last_heartbeat_at"] is not None


async def test_a_heartbeat_does_not_resurrect_a_drained_or_disabled_host(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    await hosts.set_host_status(host="runner-a", status=HOST_DRAINING)

    assert (await hosts.record_host_heartbeat(host="runner-a"))["status"] == HOST_DRAINING


async def test_re_registration_recounts_slots_rather_than_zeroing_them(
    store, hosts: PostgresHostPoolRepository
):
    """A machine rebuilt mid-job must not be told it is empty.

    Zeroing would over-subscribe it while the old attempts still hold leases;
    counting is right whether the machine was rebuilt or merely re-registered.
    """
    await _enrol(store, "runner-a", slots_total=4, hosts=hosts)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 1

    await hosts.register_host(host="runner-a", credential_hash=hash_secret("new"), slots_total=4)

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 1


# ── The status machine and the reaper ──────────────────────────────────


async def test_an_illegal_transition_is_refused_by_the_store(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    await hosts.remove_host(host="runner-a")

    assert await hosts.set_host_status(host="runner-a", status=HOST_ONLINE) == "invalid"
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_DISABLED


async def test_an_unknown_host_cannot_be_transitioned(store, hosts: PostgresHostPoolRepository):
    assert await hosts.set_host_status(host="nope", status=HOST_DRAINING) == "unknown"


async def _drain_epoch(hosts: PostgresHostPoolRepository, host: str = "runner-a"):
    row = await hosts.get_host(host=host)
    assert row is not None
    return row["drain_fence_epoch"]


async def test_drain_fence_acquire_and_release_are_response_loss_idempotent(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    initial_epoch = await _drain_epoch(hosts)
    token = uuid4()

    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    )
    assert acquired[:2] == ("ok", HOST_ONLINE)
    active_epoch = acquired[2]
    assert active_epoch != initial_epoch
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    ) == ("ok", HOST_ONLINE, active_epoch)

    released = await hosts.release_host_drain_fence(host="runner-a", fence_token=token)
    assert released[0] == "ok"
    released_epoch = released[1]
    assert released_epoch != active_epoch
    assert await hosts.release_host_drain_fence(host="runner-a", fence_token=token) == (
        "ok",
        released_epoch,
    )

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_ONLINE
    assert row["drain_fence_token"] == token
    assert row["drain_fence_status_before"] == HOST_ONLINE
    assert row["drain_fence_active"] is False
    assert row["drain_fence_epoch"] == released_epoch
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    ) == ("released", None, released_epoch)


async def test_drain_fence_requires_expected_token_and_epoch_for_transfer(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    initial_epoch = await _drain_epoch(hosts)
    first, second = uuid4(), uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=first,
    )
    assert acquired[:2] == ("ok", HOST_ONLINE)
    first_epoch = acquired[2]

    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=uuid4(),
        expected_fence_epoch=first_epoch,
        fence_token=second,
    ) == ("stale", None, first_epoch)
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=first,
        expected_fence_epoch=initial_epoch,
        fence_token=second,
    ) == ("stale", None, first_epoch)
    transferred = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=first,
        expected_fence_epoch=first_epoch,
        fence_token=second,
    )
    assert transferred[:2] == ("ok", HOST_DRAINING)
    second_epoch = transferred[2]
    assert second_epoch != first_epoch
    assert await hosts.release_host_drain_fence(host="runner-a", fence_token=first) == (
        "stale",
        second_epoch,
    )

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DRAINING
    assert row["drain_fence_token"] == second
    assert row["drain_fence_status_before"] == HOST_DRAINING
    assert row["drain_fence_active"] is True


async def test_drain_fence_refuses_manual_drain_and_legacy_undrain_without_force(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    assert await hosts.set_host_status(host="runner-a", status=HOST_DRAINING) == "ok"
    manual_epoch = await _drain_epoch(hosts)
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=manual_epoch,
        fence_token=uuid4(),
    ) == ("manual", None, manual_epoch)
    assert await hosts.set_host_status(host="runner-a", status=HOST_ONLINE) == "ok"

    token = uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=await _drain_epoch(hosts),
        fence_token=token,
    )
    assert acquired[:2] == ("ok", HOST_ONLINE)
    assert await hosts.set_host_status(host="runner-a", status=HOST_DRAINING) == "ok"
    assert await hosts.set_host_status(host="runner-a", status=HOST_ONLINE) == "fenced"
    assert await hosts.set_host_status(host="runner-a", status=HOST_ONLINE, force=True) == "ok"
    row = await hosts.get_host(host="runner-a")
    assert row["drain_fence_token"] is None
    assert row["drain_fence_status_before"] is None
    assert row["drain_fence_active"] is False


async def test_database_constraint_rejects_active_old_writes_and_partial_receipts(
    db, store, hosts: PostgresHostPoolRepository
):
    import asyncpg

    await _enrol(store, "runner-a", hosts=hosts)
    token = uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=await _drain_epoch(hosts),
        fence_token=token,
    )
    assert acquired[:2] == ("ok", HOST_ONLINE)
    with pytest.raises(asyncpg.CheckViolationError):
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_runner_hosts SET status=$2 WHERE host=$1",
                "runner-a",
                HOST_ONLINE,
            )
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_DRAINING

    await _enrol(store, "runner-b", hosts=hosts)
    with pytest.raises(asyncpg.CheckViolationError):
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_runner_hosts SET drain_fence_token=$2::uuid WHERE host=$1",
                "runner-b",
                str(uuid4()),
            )


async def test_reenrollment_cannot_clear_an_active_drain_fence(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    token = uuid4()
    await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=await _drain_epoch(hosts),
        fence_token=token,
    )

    with pytest.raises(ValueError, match="host_drain_fenced"):
        await hosts.register_host(host="runner-a", credential_hash=hash_secret("replacement"))

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DRAINING
    assert row["drain_fence_token"] == token
    assert row["drain_fence_status_before"] == HOST_ONLINE
    assert row["drain_fence_active"] is True
    assert await hosts.authenticate_host(
        host="runner-a", credential_hash=hash_secret("cred-runner-a")
    )
    assert (
        await hosts.authenticate_host(host="runner-a", credential_hash=hash_secret("replacement"))
        is None
    )


async def test_database_prevents_active_delete_and_epoch_fences_delete_recreate_replay(
    db, store, hosts: PostgresHostPoolRepository
):
    import asyncpg

    await _enrol(store, "runner-a", hosts=hosts)
    initial_epoch = await _drain_epoch(hosts)
    token = uuid4()
    await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    )

    assert await hosts.forget_runner_host(host="runner-a") == "fenced"
    with pytest.raises(asyncpg.CheckViolationError):
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM agent_runner_hosts WHERE host=$1", "runner-a")

    assert (await hosts.release_host_drain_fence(host="runner-a", fence_token=token))[0] == "ok"
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "DELETE FROM agent_runner_hosts WHERE host=$1 RETURNING host", "runner-a"
            )
            == "runner-a"
        )
    await _enrol(store, "runner-a", hosts=hosts)
    recreated_epoch = await _drain_epoch(hosts)
    assert recreated_epoch != initial_epoch
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    ) == ("stale", None, recreated_epoch)


async def test_old_writer_clear_and_heartbeat_fence_an_acquire_replay(
    db, store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    initial_epoch = await _drain_epoch(hosts)
    token = uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    )
    assert acquired[0] == "ok"
    released = await hosts.release_host_drain_fence(host="runner-a", fence_token=token)
    assert released[0] == "ok"

    async with db.acquire() as conn:
        cleared_epoch = await conn.fetchval(
            """
            UPDATE agent_runner_hosts
            SET status=$2, drain_fence_token=NULL,
                drain_fence_status_before=NULL, drain_fence_active=FALSE
            WHERE host=$1
            RETURNING drain_fence_epoch
            """,
            "runner-a",
            HOST_OFFLINE,
        )
        heartbeat_epoch = await conn.fetchval(
            "UPDATE agent_runner_hosts SET status=$2 WHERE host=$1 RETURNING drain_fence_epoch",
            "runner-a",
            HOST_ONLINE,
        )
    assert cleared_epoch not in (acquired[2], released[1])
    assert heartbeat_epoch != cleared_epoch
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=initial_epoch,
        fence_token=token,
    ) == ("stale", None, heartbeat_epoch)


async def test_released_receipt_survives_old_writer_status_change(
    db, store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    token = uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=await _drain_epoch(hosts),
        fence_token=token,
    )
    released = await hosts.release_host_drain_fence(host="runner-a", fence_token=token)
    assert acquired[0] == released[0] == "ok"

    async with db.acquire() as conn:
        offline_epoch = await conn.fetchval(
            "UPDATE agent_runner_hosts SET status=$2 WHERE host=$1 RETURNING drain_fence_epoch",
            "runner-a",
            HOST_OFFLINE,
        )
    assert offline_epoch != released[1]
    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=uuid4(),
        expected_fence_epoch=uuid4(),
        fence_token=token,
    ) == ("released", None, offline_epoch)
    assert await hosts.release_host_drain_fence(host="runner-a", fence_token=token) == (
        "ok",
        offline_epoch,
    )


async def test_inactive_receipt_does_not_turn_a_manual_drain_into_owned_drain(
    db, store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    receipt_token = uuid4()
    acquired = await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=None,
        expected_fence_epoch=await _drain_epoch(hosts),
        fence_token=receipt_token,
    )
    assert acquired[0] == "ok"
    released = await hosts.release_host_drain_fence(host="runner-a", fence_token=receipt_token)
    assert released[0] == "ok"

    async with db.acquire() as conn:
        manual_epoch = await conn.fetchval(
            "UPDATE agent_runner_hosts SET status=$2 WHERE host=$1 RETURNING drain_fence_epoch",
            "runner-a",
            HOST_DRAINING,
        )

    assert await hosts.acquire_host_drain_fence(
        host="runner-a",
        expected_fence_token=receipt_token,
        expected_fence_epoch=manual_epoch,
        fence_token=uuid4(),
    ) == ("manual", None, manual_epoch)
    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DRAINING
    assert row["drain_fence_token"] == receipt_token
    assert row["drain_fence_active"] is False


async def test_the_reaper_marks_a_silent_enrolled_host_offline(
    db, store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    await _age_heartbeat(db, store, "runner-a", seconds=600)

    reaped = await hosts.reap_offline_hosts(interval_seconds=30, missed_intervals=3)

    assert reaped == ["runner-a"]
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_OFFLINE


async def test_the_reaper_leaves_a_recently_seen_host_alone(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)

    assert await hosts.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == []


async def test_a_drained_host_stays_drained_when_it_goes_quiet_and_returns(
    db, store, hosts: PostgresHostPoolRepository
):
    """The operator's instruction must survive the machine's absence.

    This failed before: the reaper moved ``draining -> offline``, and the next
    heartbeat moved ``offline -> online``, because a machine returning from a
    partition is meant to come back without an operator. Composed, that made a
    drained host rejoin the claim pool by disappearing and coming back —
    quietly taking the work the operator had just decided it must not take.

    Drain is an instruction, not an observation. Nothing overwrites it.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    await hosts.set_host_status(host="runner-a", status=HOST_DRAINING)
    await _age_heartbeat(db, store, "runner-a", seconds=600)

    assert await hosts.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == []
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_DRAINING

    # And the return does not promote it either.
    await hosts.record_host_heartbeat(host="runner-a")
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_DRAINING


async def test_a_host_that_registered_and_never_came_up_ages_out_of_enrolling(
    db, store, hosts: PostgresHostPoolRepository
):
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("cred"))
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET first_seen_at = NOW() - INTERVAL '1 hour' "
            "WHERE host = 'runner-a'"
        )

    assert await hosts.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == ["runner-a"]


async def test_a_disabled_host_is_not_reaped_again(db, store, hosts: PostgresHostPoolRepository):
    """Nothing to say: it is already out, and re-reporting it is noise."""
    await _enrol(store, "runner-a", hosts=hosts)
    await hosts.remove_host(host="runner-a")
    await _age_heartbeat(db, store, "runner-a", seconds=600)

    assert await hosts.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == []


async def _age_heartbeat(db, store: Storage, host: str, *, seconds: int) -> None:
    """Push a host's last heartbeat into the past, without sleeping."""
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts "
            "SET last_heartbeat_at = NOW() - make_interval(secs => $2) "
            "WHERE host = $1",
            host,
            seconds,
        )


# ── Host-aware claiming ────────────────────────────────────────────────


async def test_a_claim_takes_a_slot_and_stamps_the_attempt(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)

    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None
    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 1
    assert (await hosts.host_for_attempt(attempt_id=claim["attempt_id"]))["host"] == "runner-a"


async def test_a_heartbeat_round_trips_the_version_stamp(store, hosts: PostgresHostPoolRepository):
    """'' and {} are answers, and a stampless beat does not erase them."""
    await _enrol(store, "runner-a", hosts=hosts)
    row = await hosts.record_host_heartbeat(
        host="runner-a",
        sandbox_runtime="",
        sandbox_image_digest="sha256:abc",
        runtime_versions={},
    )
    assert row["sandbox_runtime"] == ""
    assert row["sandbox_image_digest"] == "sha256:abc"
    assert row["runtime_versions"] == {}

    row = await hosts.record_host_heartbeat(host="runner-a")

    assert row["sandbox_runtime"] == ""
    assert row["sandbox_image_digest"] == "sha256:abc"
    assert row["runtime_versions"] == {}


async def test_a_claim_stamps_its_replicas_attestation_not_the_host_heartbeat(
    db, store, hosts: PostgresHostPoolRepository
):
    """Rolling replicas sharing a host row are judged and stamped independently."""
    await _enrol(store, "runner-a", hosts=hosts)
    await hosts.record_host_heartbeat(
        host="runner-a",
        sandbox_runtime="",
        sandbox_image_digest="sha256:old-host-row",
        runtime_versions={"claude-code": "0.9.0"},
    )
    await _create_job(store, execution_profile="untrusted-kata-v1")

    claim = await _claim(
        store,
        worker_id="w1",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("cred-runner-a"),
        claim_attestation=_kata_attestation(),
    )

    assert claim is not None
    async with db.acquire() as conn:
        attempt = await conn.fetchrow(
            "SELECT sandbox_runtime, sandbox_image_digest, runtime_version, "
            "host_kernel, guest_kernel "
            "FROM agent_attempts WHERE id = $1",
            claim["attempt_id"],
        )
    assert attempt["sandbox_runtime"] == "io.containerd.kata.v2"
    assert attempt["sandbox_image_digest"] == "sandbox@sha256:claim"
    assert attempt["runtime_version"] == "2.1.220"
    assert attempt["host_kernel"] == "6.8.0-host"
    assert attempt["guest_kernel"] == "6.1.0-kata"

    await hosts.record_host_heartbeat(
        host="runner-a",
        sandbox_runtime="io.containerd.kata.v2",
        sandbox_image_digest="sha256:newer-host-row",
        runtime_versions={"claude-code": "3.0.0"},
    )
    async with db.acquire() as conn:
        unchanged = await conn.fetchrow(
            "SELECT sandbox_image_digest, runtime_version FROM agent_attempts WHERE id = $1",
            claim["attempt_id"],
        )
    assert dict(unchanged) == {
        "sandbox_image_digest": "sandbox@sha256:claim",
        "runtime_version": "2.1.220",
    }

    # Trusted execution changes the isolation profile, not the retained
    # session protocol: it still needs an exact shared-session attestation.
    await _enrol(store, "runner-b", hosts=hosts)
    await _create_job(store)
    trusted = await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-b")
    assert trusted is not None
    async with db.acquire() as conn:
        attempt = await conn.fetchrow(
            "SELECT sandbox_runtime, sandbox_image_digest, runtime_version "
            "FROM agent_attempts WHERE id = $1",
            trusted["attempt_id"],
        )
    assert attempt["sandbox_runtime"] == ""
    assert attempt["runtime_version"] == "2.1.220"


@pytest.mark.parametrize(
    "attestation",
    [
        {},
        _shared_attestation(runner_capabilities=[]),
        _shared_attestation(
            runner_capabilities=[
                "shared-sandbox-v1",
                shared_deployment_capability("test-deployment"),
            ]
        ),
        _shared_attestation(sandbox_image_digest=""),
        _shared_attestation(
            helper_versions={**SHARED_SANDBOX_HELPER_PINS, "claude-session-fork": "1"}
        ),
        _shared_attestation(
            helper_versions={
                "claude-agent-sdk": "0.2.147",
                "guest-observer": "1",
                "tool-mark": "1",
            }
        ),
        _shared_attestation(
            runtime_versions={
                "claude-code": "2.1.219",
                "codex": "0.145.0",
                "pi": "0.73.1",
                "opencode": "1.18.9",
                "kilo": "7.4.17",
            }
        ),
    ],
)
async def test_shared_host_row_cannot_authorize_a_mismatched_polling_replica(
    store, attestation, hosts
):
    """One upgraded sibling's heartbeat is not another replica's claim proof."""
    await _enrol(store, "runner-a", capabilities=["shared-sandbox-v1"], slots_total=2, hosts=hosts)
    job = await _create_job(store, metadata={"requires": ["shared-sandbox-v1"]})

    claim = await _claim(
        store,
        worker_id="rolling-old-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=attestation,
    )

    assert claim is None
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "queued"


async def test_exact_polling_replica_can_claim_shared_work(store, hosts):
    await _enrol(store, "runner-a", capabilities=["shared-sandbox-v1"], slots_total=2, hosts=hosts)
    job = await _create_job(store, metadata={"requires": ["shared-sandbox-v1"]})

    claim = await _claim(
        store,
        worker_id="rolling-new-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=_shared_attestation(),
    )

    assert claim is not None and claim["id"] == job["id"]


async def test_new_group_claim_requires_the_control_planes_exact_deployment(store, hosts):
    await _enrol(store, "runner-a", capabilities=["shared-sandbox-v1"], slots_total=2, hosts=hosts)
    job = await _create_job(store, metadata={"requires": ["shared-sandbox-v1"]})
    stale = _shared_attestation(
        runner_capabilities=[
            "shared-sandbox-v1",
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability("previous-deployment"),
        ]
    )

    refused = await _claim(
        store,
        worker_id="pending-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=stale,
        expected_deployment_sha="target-deployment",
    )
    accepted = await _claim(
        store,
        worker_id="ready-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=_shared_attestation(
            runner_capabilities=[
                "shared-sandbox-v1",
                NATIVE_FORK_AT_ANCHOR_CAPABILITY,
                shared_deployment_capability("target-deployment"),
            ]
        ),
        expected_deployment_sha="target-deployment",
    )

    assert refused is None
    assert accepted is not None and accepted["id"] == job["id"]


async def test_runc_replica_skips_secure_work_and_takes_the_next_trusted_job(store, hosts):
    await _enrol(store, "runner-a", capabilities=["kata"], hosts=hosts)
    secure = await _create_job(store, execution_profile="untrusted-kata-v1")
    trusted = await _create_job(store, task_prompt="trusted", execution_profile="trusted-v1")

    claim = await _claim(
        store,
        worker_id="runc-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("cred-runner-a"),
        claim_attestation=_kata_attestation(sandbox_runtime="", guest_kernel=None),
    )

    assert claim is not None and claim["id"] == trusted["id"]
    assert (await store.conversation_queries.get_job(secure["id"]))["state"] == "queued"


@pytest.mark.parametrize(
    "attestation",
    [
        {},
        _kata_attestation(sandbox_runtime="", guest_kernel=None),
        _kata_attestation(sandbox_runtime="runsc"),
        _kata_attestation(guest_kernel="6.8.0-host"),
        _kata_attestation(guest_kernel=""),
    ],
)
async def test_capability_labels_or_arbitrary_runtime_cannot_claim_secure_work(
    store, attestation, hosts
):
    await _enrol(store, "runner-a", capabilities=["kata"], hosts=hosts)
    await _create_job(store, execution_profile="untrusted-kata-v1")

    assert (
        await _claim(
            store,
            worker_id="w1",
            lease_ttl_seconds=60,
            host="runner-a",
            host_credential_hash=hash_secret("cred-runner-a"),
            claim_attestation=attestation,
        )
        is None
    )


@pytest.mark.parametrize("disk_limit_mib", [0, 20480, None])
async def test_a_new_secure_group_is_claimed_whatever_disk_quota_is_attested(
    store, hosts, disk_limit_mib
):
    """The disk quota is a recommendation: it warns, it never changes scheduling.

    ``None`` is a runner from before the report.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    secure = await _create_job(store, execution_profile="untrusted-kata-v1")
    attestation = (
        _kata_attestation()
        if disk_limit_mib is None
        else _kata_attestation(sandbox_disk_limit_mib=disk_limit_mib)
    )

    claim = await _claim(
        store,
        worker_id="w1",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=attestation,
    )

    assert claim is not None and claim["id"] == secure["id"]


async def test_shared_or_superseded_host_credential_cannot_claim_secure_work(store, hosts):
    await _enrol(store, "runner-a", hosts=hosts)
    job = await _create_job(store, execution_profile="untrusted-kata-v1")

    shared = await _claim(
        store,
        worker_id="shared-dispatcher",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash="",
        claim_attestation=_kata_attestation(),
    )
    stale = await _claim(
        store,
        worker_id="stale-host-token",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("wrong"),
        claim_attestation=_kata_attestation(),
    )
    current = await _claim(
        store,
        worker_id="current-host-token",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("cred-runner-a"),
        claim_attestation=_kata_attestation(),
    )

    assert shared is None
    assert stale is None
    assert current is not None and current["id"] == job["id"]


async def test_reenrollment_revokes_an_already_authenticated_claim_generation(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    old_hash = hash_secret("cred-runner-a")
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("replacement"))
    await hosts.record_host_heartbeat(host="runner-a")
    await _create_job(store, execution_profile="untrusted-kata-v1")

    assert (
        await _claim(
            store,
            worker_id="old",
            lease_ttl_seconds=60,
            host="runner-a",
            host_credential_hash=old_hash,
            claim_attestation=_kata_attestation(),
        )
        is None
    )
    assert await _claim(
        store,
        worker_id="new",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("replacement"),
        claim_attestation=_kata_attestation(),
    )


async def test_a_full_host_claims_nothing_more(store, hosts):
    await _enrol(store, "runner-a", slots_total=1, hosts=hosts)
    await _create_job(store)
    await _create_job(store)

    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    second = await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    assert first is not None
    assert second is None


async def test_the_job_a_full_host_could_not_take_is_still_queued(db, store, hosts):
    """Refusing must not consume the job, or capacity becomes data loss."""
    await _enrol(store, "runner-a", slots_total=1, hosts=hosts)
    job = await _create_job(store)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    states = {job["id"]: (await store.conversation_queries.get_job(job["id"]))["state"]}
    assert "queued" in states.values() or states[job["id"]] == "running"
    async with db.acquire() as conn:
        queued = await conn.fetchval("SELECT count(*) FROM agent_jobs WHERE state = 'queued'")
    assert queued == 1


async def test_an_unmetered_host_keeps_claiming(store, hosts: PostgresHostPoolRepository):
    """Null slots_total is the pre-pool shape and must stay unlimited."""
    await _enrol(store, "runner-a", hosts=hosts)
    for _ in range(3):
        await _create_job(store)

    claims = [
        await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a")
        for n in range(3)
    ]

    assert all(claim is not None for claim in claims)
    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 3


async def test_a_requested_size_below_the_reported_count_caps_claims_at_once(
    store, hosts: PostgresHostPoolRepository
):
    """The claim honours the lower of what the host reports and what was asked for."""
    await _enrol(store, "runner-a", slots_total=3, hosts=hosts)
    assert await hosts.set_host_slots_desired(host="runner-a", slots=2) == "ok"
    for _ in range(3):
        await _create_job(store)

    claims = [
        await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a")
        for n in range(3)
    ]

    assert [claim is not None for claim in claims] == [True, True, False]
    host = await hosts.get_host(host="runner-a")
    assert host["slots_used"] == 2
    # The report is untouched: the request sits beside it, not over it.
    assert (host["slots_total"], host["slots_desired"]) == (3, 2)


async def test_a_requested_size_above_the_reported_count_admits_nothing_extra(
    store, hosts: PostgresHostPoolRepository
):
    """Until the host has grown into the request, the executors it reports bound it."""
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    assert await hosts.set_host_slots_desired(host="runner-a", slots=4) == "ok"
    for _ in range(4):
        await _create_job(store)

    claims = [
        await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a")
        for n in range(3)
    ]
    assert [claim is not None for claim in claims] == [True, True, False]

    # The host's next heartbeat reports the executors it spawned.
    await hosts.record_host_heartbeat(host="runner-a", slots_total=4)

    assert await _claim(store, worker_id="w3", lease_ttl_seconds=60, host="runner-a") is not None
    assert await _claim(store, worker_id="w4", lease_ttl_seconds=60, host="runner-a") is not None
    assert await _claim(store, worker_id="w5", lease_ttl_seconds=60, host="runner-a") is None


async def test_clearing_the_request_hands_back_the_reported_capacity(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", slots_total=3, hosts=hosts)
    await hosts.set_host_slots_desired(host="runner-a", slots=2)
    for _ in range(3):
        await _create_job(store)
    for n in range(2):
        assert await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a")
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a") is None

    assert await hosts.set_host_slots_desired(host="runner-a", slots=None) == "ok"

    assert await _claim(store, worker_id="w3", lease_ttl_seconds=60, host="runner-a") is not None


async def test_an_unmetered_host_is_bounded_by_the_request_alone(
    store, hosts: PostgresHostPoolRepository
):
    """No report means nothing to compare; the request is then the only bound."""
    await _enrol(store, "runner-a", hosts=hosts)
    assert await hosts.set_host_slots_desired(host="runner-a", slots=2) == "ok"
    for _ in range(3):
        await _create_job(store)

    claims = [
        await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a")
        for n in range(3)
    ]

    assert [claim is not None for claim in claims] == [True, True, False]


async def test_sizing_an_unknown_host_writes_nothing(store, hosts: PostgresHostPoolRepository):
    assert await hosts.set_host_slots_desired(host="nope", slots=2) == "unknown"


async def test_the_request_survives_heartbeats_and_re_enrollment(
    store, hosts: PostgresHostPoolRepository
):
    """It is a decision about the named machine, not a field the host reports."""
    await _enrol(store, "runner-a", slots_total=4, hosts=hosts)
    await hosts.set_host_slots_desired(host="runner-a", slots=2)

    await hosts.record_host_heartbeat(host="runner-a", slots_total=4)
    assert (await hosts.get_host(host="runner-a"))["slots_desired"] == 2

    await hosts.register_host(host="runner-a", credential_hash=hash_secret("new"), slots_total=4)
    assert (await hosts.get_host(host="runner-a"))["slots_desired"] == 2


async def test_the_schema_refuses_a_non_positive_request(db, store, hosts):
    """A size of zero is not a size; the column says so before any code does."""
    import asyncpg

    await _enrol(store, "runner-a", slots_total=3, hosts=hosts)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET slots_desired = 0 WHERE host = 'runner-a'"
            )


async def test_global_host_settings_change_one_key_at_a_time(
    store, hosts: PostgresHostPoolRepository
):
    """Setting one value leaves the others; None clears only the one named."""
    assert await hosts.host_settings() == {}

    assert await hosts.set_host_settings(changes={"agent_timeout_s": 7200}) == {}
    previous = await hosts.set_host_settings(
        changes={"terminal_max_sessions": 32, "agent_timeout_s": 900}
    )
    assert previous == {"agent_timeout_s": 7200}
    assert await hosts.host_settings() == {"agent_timeout_s": 900, "terminal_max_sessions": 32}

    await hosts.set_host_settings(changes={"agent_timeout_s": None})
    assert await hosts.host_settings() == {"terminal_max_sessions": 32}


async def test_global_host_settings_survive_a_missing_policy_row(
    db, store, hosts: PostgresHostPoolRepository
):
    """An upsert, like the pin: a bare UPDATE would report success and store nothing."""
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM agent_runner_policy")

    await hosts.set_host_settings(changes={"chat_idle_timeout_s": 600})

    assert await hosts.host_settings() == {"chat_idle_timeout_s": 600}


async def test_archive_policy_cas_is_noop_and_lost_response_idempotent(
    hosts: PostgresHostPoolRepository,
):
    initial = await hosts.sandbox_archive_settings()
    assert initial["revision"] == 0
    assert initial["s3_uri"] == SANDBOX_ARCHIVE_S3_URI

    changed = await hosts.set_sandbox_archive_settings(
        expected_revision=0,
        settings=_archive_settings(),
    )
    assert changed is not None and changed["revision"] == 1 and changed["_changed"] is True

    no_op = await hosts.set_sandbox_archive_settings(
        expected_revision=1,
        settings=_archive_settings(),
    )
    retry = await hosts.set_sandbox_archive_settings(
        expected_revision=0,
        settings=_archive_settings(),
    )
    assert no_op is not None and retry is not None
    assert no_op["revision"] == retry["revision"] == 1
    assert no_op["updated_at"] == retry["updated_at"] == changed["updated_at"]
    assert no_op["_changed"] is retry["_changed"] is False

    conflict = await hosts.set_sandbox_archive_settings(
        expected_revision=0,
        settings=_archive_settings(max_archive_bytes=1024**3),
    )
    assert conflict is None
    future = await hosts.set_sandbox_archive_settings(
        expected_revision=2,
        settings=_archive_settings(),
    )
    assert future is None


async def test_concurrent_identical_archive_policy_writes_increment_once(
    hosts: PostgresHostPoolRepository,
):
    results = await asyncio.gather(
        *(
            hosts.set_sandbox_archive_settings(
                expected_revision=0,
                settings=_archive_settings(),
            )
            for _ in range(8)
        )
    )

    assert all(result is not None and result["revision"] == 1 for result in results)
    assert sum(result["_changed"] is True for result in results if result is not None) == 1


async def test_archive_policy_missing_singleton_reads_and_recreates_cleanly(
    db,
    hosts: PostgresHostPoolRepository,
):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM agent_runner_policy WHERE id")

    missing = await hosts.sandbox_archive_settings()
    assert missing["revision"] == 0
    assert missing["updated_at"] is None

    restored = await hosts.set_sandbox_archive_settings(
        expected_revision=0,
        settings=_archive_settings(),
    )
    assert restored is not None and restored["revision"] == 1
    assert restored["updated_at"] is not None


async def test_archive_revision_heartbeat_is_tristate_and_credential_fenced(
    hosts: PostgresHostPoolRepository,
):
    old_hash = hash_secret("old")
    new_hash = hash_secret("new")
    await hosts.register_host(host="runner-a", credential_hash=old_hash)
    reported = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=old_hash,
        sandbox_archive_settings_applied_revision=5,
        sandbox_archive_settings_revision_reported=True,
    )
    assert reported is not None
    assert reported["sandbox_archive_settings_applied_revision"] == 5

    omitted = await hosts.record_host_heartbeat(host="runner-a", credential_hash=old_hash)
    assert omitted is not None
    assert omitted["sandbox_archive_settings_applied_revision"] == 5
    cleared = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=old_hash,
        sandbox_archive_settings_applied_revision=None,
        sandbox_archive_settings_revision_reported=True,
    )
    assert cleared is not None
    assert cleared["sandbox_archive_settings_applied_revision"] is None

    await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=old_hash,
        sandbox_archive_settings_applied_revision=6,
        sandbox_archive_settings_revision_reported=True,
    )
    reenrolled = await hosts.register_host(host="runner-a", credential_hash=new_hash)
    assert reenrolled["sandbox_archive_settings_applied_revision"] is None

    # A beat from the replaced credential is refused whole: it neither
    # confirms an archive revision nor counts as the new machine's liveness.
    stale = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=old_hash,
        sandbox_archive_settings_applied_revision=7,
        sandbox_archive_settings_revision_reported=True,
    )
    assert stale is None
    row = await hosts.get_host(host="runner-a")
    assert row["sandbox_archive_settings_applied_revision"] is None
    assert row["last_heartbeat_at"] is None
    assert row["status"] == HOST_ENROLLING
    current = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=new_hash,
        sandbox_archive_settings_applied_revision=7,
        sandbox_archive_settings_revision_reported=True,
    )
    assert current is not None
    assert current["sandbox_archive_settings_applied_revision"] == 7


async def test_a_beat_that_authenticated_before_a_removal_changes_nothing(
    hosts: PostgresHostPoolRepository,
):
    """Authentication and the write are two statements; the write re-checks.

    The beat below authenticated while the credential was live and reached
    the store after the removal committed. It must not stamp the removed row
    with liveness, a sandbox manager address or capabilities.
    """
    digest = hash_secret("cred-runner-a")
    await hosts.register_host(
        host="runner-a", credential_hash=digest, sandbox_manager_url="http://runner-a:8787"
    )
    assert await hosts.record_host_heartbeat(host="runner-a", credential_hash=digest) is not None
    before = await hosts.get_host(host="runner-a")
    assert await hosts.remove_host(host="runner-a") == "ok"

    late = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=digest,
        sandbox_manager_url="http://elsewhere:8787",
        capabilities=["shared-sandbox-v1"],
    )

    assert late is None
    after = await hosts.get_host(host="runner-a")
    assert after["status"] == HOST_DISABLED
    assert after["sandbox_manager_url"] == "http://runner-a:8787"
    assert after["last_heartbeat_at"] == before["last_heartbeat_at"]
    assert after["capabilities"] == before["capabilities"]


async def test_server_settings_are_kept_apart_from_what_the_host_reports(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)
    row = await hosts.get_host(host="runner-a")
    assert (row["settings_desired"], row["settings_applied"], row["settings_local"]) == (
        {},
        None,
        None,
    )

    assert (
        await hosts.set_runner_host_settings(
            host="runner-a", changes={"source_import_max_mib": 4096}
        )
        == {}
    )
    await hosts.record_host_heartbeat(
        host="runner-a",
        settings_applied={"source_import_max_mib": 4096},
        settings_local={"source_import_max_mib": 1024},
    )

    row = await hosts.get_host(host="runner-a")
    assert row["settings_desired"] == {"source_import_max_mib": 4096}
    assert row["settings_applied"] == {"source_import_max_mib": 4096}
    assert row["settings_local"] == {"source_import_max_mib": 1024}

    # A beat that does not report settings leaves the last report in place.
    await hosts.record_host_heartbeat(host="runner-a", slots_total=3)
    assert (await hosts.get_host(host="runner-a"))["settings_applied"] == {
        "source_import_max_mib": 4096
    }


async def test_server_settings_survive_re_enrollment_and_the_report_does_not(
    store, hosts: PostgresHostPoolRepository
):
    """The admin's values are about the name; the report is about the runner behind it."""
    await _enrol(store, "runner-a", hosts=hosts)
    await hosts.set_runner_host_settings(host="runner-a", changes={"agent_timeout_s": 900})
    await hosts.record_host_heartbeat(
        host="runner-a",
        settings_applied={"agent_timeout_s": 900},
        settings_local={"agent_timeout_s": 3600},
    )

    await hosts.register_host(host="runner-a", credential_hash=hash_secret("new"))

    row = await hosts.get_host(host="runner-a")
    assert row["settings_desired"] == {"agent_timeout_s": 900}
    assert (row["settings_applied"], row["settings_local"]) == (None, None)


async def test_setting_an_unknown_server_writes_nothing(store, hosts: PostgresHostPoolRepository):
    assert (
        await hosts.set_runner_host_settings(host="nope", changes={"agent_timeout_s": 900}) is None
    )


async def test_the_schema_refuses_settings_that_are_not_an_object(db, store, hosts):
    import asyncpg

    await _enrol(store, "runner-a", hosts=hosts)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET settings_desired = '[]'::jsonb "
                "WHERE host = 'runner-a'"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE agent_runner_policy SET host_settings = '1'::jsonb")


async def test_a_host_reports_why_it_is_not_claiming_and_can_clear_it(
    store, hosts: PostgresHostPoolRepository
):
    """A machine with room and no work needs a reason on the page, not a guess.

    ``""`` is the host saying nothing is wrong and has to be able to clear a
    stale reason; omitting the field is a host that does not report this at
    all and must leave what is stored alone.
    """
    await _enrol(store, "runner-a", slots_total=3, hosts=hosts)

    await hosts.record_host_heartbeat(host="runner-a", launch_error="3 turns in a row failed")
    assert (await hosts.get_host(host="runner-a"))["launch_error"] == "3 turns in a row failed"

    await hosts.record_host_heartbeat(host="runner-a", slots_total=3)
    assert (await hosts.get_host(host="runner-a"))["launch_error"] == "3 turns in a row failed", (
        "an unreported field changes nothing"
    )

    await hosts.record_host_heartbeat(host="runner-a", launch_error="")
    assert (await hosts.get_host(host="runner-a"))["launch_error"] == ""


async def test_a_draining_host_receives_no_new_claims(store, hosts: PostgresHostPoolRepository):
    await _create_job(store)
    await hosts.set_host_status(host="runner-a", status=HOST_DRAINING)

    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None


@pytest.mark.parametrize("status", [HOST_ENROLLING, HOST_OFFLINE, HOST_DISABLED])
async def test_only_an_online_host_claims(db, store, status, hosts):
    await _enrol(store, "runner-a", slots_total=4, hosts=hosts)
    await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET status = $2 WHERE host = $1", "runner-a", status
        )

    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None


async def test_a_host_missing_a_required_capability_skips_that_job(store, hosts):
    """And takes the next one it *can* run, rather than stalling behind it."""
    await _enrol(store, "runner-a", slots_total=4, capabilities=["runc"], hosts=hosts)
    await _create_job(store, metadata={"requires": ["kata"]})
    runnable = await _create_job(store, task_prompt="the plain one")

    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None
    assert claim["id"] == runnable["id"]


async def test_a_host_declaring_a_superset_takes_the_job(store, hosts):
    await _enrol(
        store, "runner-a", slots_total=4, capabilities=["kata", "gpu", "runc"], hosts=hosts
    )
    job = await _create_job(store, metadata={"requires": ["kata", "gpu"]})

    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None
    assert claim["id"] == job["id"]


async def test_a_runner_without_an_enrolled_host_never_claims(store):
    """The new topology has no anonymous/single-host claim fallback."""
    await _create_job(store)

    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60) is None


async def test_finishing_a_job_gives_the_slot_back(db, store, hosts: PostgresHostPoolRepository):
    await _enrol(store, "runner-a", slots_total=1, hosts=hosts)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    await _settled(db, store, claim["id"])
    await store.attempt_commands.transition(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 0


async def test_releasing_an_unstarted_claim_gives_the_slot_back(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", slots_total=1, hosts=hosts)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    await store.attempt_commands.release_claim(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 0


async def test_a_reaped_attempt_gives_the_slot_back(store, hosts: PostgresHostPoolRepository):
    await _enrol(store, "runner-a", slots_total=1, hosts=hosts)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=-1, host="runner-a")

    await store.attempt_commands.reap_expired(max_attempts=3)

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 0


async def test_a_double_terminal_transition_releases_one_slot_not_two(
    db, store, hosts: PostgresHostPoolRepository
):
    """The counter must survive a caller that retries a finish."""
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    for _ in range(3):
        await _settled(db, store, first["id"])
        await store.attempt_commands.transition(
            job_id=first["id"],
            attempt_id=first["attempt_id"],
            lease_generation=first["lease_generation"],
            from_states=("running",),
            to_state="succeeded",
        )

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 1


async def test_reaping_and_finishing_the_same_attempt_releases_one_slot(
    db, store, hosts: PostgresHostPoolRepository
):
    """Two paths out of ``running`` that can both be attempted for one attempt."""
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=-1, host="runner-a")
    await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    await store.attempt_commands.reap_expired(max_attempts=3)
    await _settled(db, store, first["id"])
    await store.attempt_commands.transition(
        job_id=first["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.attempt_commands.release_claim(
        job_id=first["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
    )

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 1


async def test_slots_never_go_negative_under_concurrent_release(
    db, store, hosts: PostgresHostPoolRepository
):
    """The invariant, hammered.

    A counter that drifts below zero makes a host look permanently
    over-committed and quietly stops it claiming, with nothing in the logs to
    say why. Guarded three times over — the release fires only on the UPDATE
    that actually closed the attempt, the decrement has a ``slots_used > 0``
    predicate, and the column carries a CHECK — so this asserts on the outcome
    of all three together.
    """
    await _enrol(store, "runner-a", slots_total=4, hosts=hosts)
    claims = []
    for _ in range(4):
        await _create_job(store)
        claims.append(await _claim(store, worker_id="w", lease_ttl_seconds=60, host="runner-a"))
    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 4

    # Every exit path, several times each, all at once.
    for claim in claims:
        await _settled(db, store, claim["id"])
    await asyncio.gather(
        *[
            store.attempt_commands.transition(
                job_id=claim["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                from_states=("running",),
                to_state="succeeded",
            )
            for claim in claims
            for _ in range(3)
        ],
        *[
            store.attempt_commands.release_claim(
                job_id=claim["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
            )
            for claim in claims
            for _ in range(3)
        ],
        *[store.attempt_commands.reap_expired(max_attempts=3) for _ in range(3)],
    )

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 0


async def test_concurrent_claims_never_oversubscribe_a_host(
    store, hosts: PostgresHostPoolRepository
):
    """Capacity is decided by the UPDATE, not by the read that preceded it.

    Eight runners polling a two-slot host at once is the ordinary steady state
    of a replica set, and a check-then-increment hands out eight.
    """
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    for _ in range(8):
        await _create_job(store)

    claims = await asyncio.gather(
        *(_claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a") for n in range(8))
    )

    granted = [claim for claim in claims if claim is not None]
    assert len(granted) == 2
    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 2


async def test_the_release_helper_refuses_to_decrement_past_zero(
    store, db, hosts: PostgresHostPoolRepository
):
    """Tested directly, because nothing above can currently reach it twice.

    The job-level fence in ``transition`` already makes a second terminal
    transition impossible, so a test driving the public API cannot distinguish
    a guarded release from an unguarded one — and the guard would rot unnoticed
    until some future caller released a slot on a path the fence does not
    cover. Calling it straight is the only way to assert what it promises.

    It is called as a statement rather than as a method because that is what it
    now is: it writes ``agent_runner_hosts`` but every caller is an attempt
    leaving ``running``, so it belongs to the host tables and to no capability.
    """
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    async with db.acquire() as conn:
        for _ in range(5):
            await release_host_slot(conn, "runner-a")

    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 0


async def test_releasing_a_slot_for_no_host_is_a_no_op(db):
    """The hostless-runner path reaches this with ``None`` on every finish."""
    async with db.acquire() as conn:
        await release_host_slot(conn, None)


async def test_the_schema_itself_refuses_a_negative_slot_count(db, store, hosts):
    """The last line of defence, asserted directly.

    If a future writer forgets the ``slots_used > 0`` predicate, this is what
    turns a silently-stuck host into a loud failure at the write that caused it.
    """
    import asyncpg

    await _enrol(store, "runner-a", slots_total=1, hosts=hosts)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET slots_used = -1 WHERE host = 'runner-a'"
            )


async def test_the_schema_refuses_a_status_it_does_not_know(db, store, hosts):
    import asyncpg

    await _enrol(store, "runner-a", hosts=hosts)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = 'sleepy' WHERE host = 'runner-a'"
            )


# ── The #1158 interaction ──────────────────────────────────────────────


async def test_removing_keeps_the_row_and_forgetting_drops_it(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", hosts=hosts)

    assert await hosts.remove_host(host="runner-a") == "ok"
    assert await hosts.get_host(host="runner-a") is not None
    assert await hosts.forget_runner_host(host="runner-a") == "deleted"
    assert await hosts.get_host(host="runner-a") is None


async def test_removing_a_busy_host_drains_it_and_the_reaper_finishes_the_job(
    db, store, hosts: PostgresHostPoolRepository
):
    """No new claims at once; revoked only after the running turn has ended."""
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    await _create_job(store)

    assert await hosts.remove_host(host="runner-a") == "draining"

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DRAINING
    assert row["removal_requested_at"] is not None
    # A second Remove while waiting keeps the original request time.
    assert await hosts.remove_host(host="runner-a") == "draining"
    assert (await hosts.get_host(host="runner-a"))["removal_requested_at"] == row[
        "removal_requested_at"
    ]
    # The queued job is not handed to a host on its way out.
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a") is None
    # Nor do heartbeats or the offline reaper undo it.
    await hosts.record_host_heartbeat(host="runner-a")
    assert await hosts.reap_offline_hosts(interval_seconds=0, missed_intervals=0) == []
    assert (await hosts.get_host(host="runner-a"))["status"] == HOST_DRAINING
    # Still busy: the finishing pass leaves it alone.
    assert await hosts.finish_host_removals() == []

    await _settled(db, store, claim["id"])
    await store.attempt_commands.transition(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert await hosts.finish_host_removals() == ["runner-a"]
    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DISABLED
    assert row["enrolled"] is False
    assert row["removal_requested_at"] is None
    assert await hosts.finish_host_removals() == []


async def test_forcing_a_busy_removal_revokes_at_once(store, hosts: PostgresHostPoolRepository):
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    assert await hosts.remove_host(host="runner-a") == "draining"

    assert await hosts.remove_host(host="runner-a", force=True) == "ok"

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_DISABLED
    assert row["removal_requested_at"] is None


async def test_putting_a_removing_host_back_to_work_cancels_the_removal(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    assert await hosts.remove_host(host="runner-a") == "draining"

    assert await hosts.set_host_status(host="runner-a", status=HOST_ONLINE) == "ok"

    row = await hosts.get_host(host="runner-a")
    assert row["status"] == HOST_ONLINE
    assert row["removal_requested_at"] is None


async def test_re_enrolling_a_removing_host_starts_it_afresh(
    store, hosts: PostgresHostPoolRepository
):
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    assert await hosts.remove_host(host="runner-a") == "draining"

    row = await hosts.register_host(host="runner-a", credential_hash=hash_secret("cred-new"))

    assert row["status"] == HOST_ENROLLING
    assert row["removal_requested_at"] is None


async def test_only_a_draining_host_can_be_waiting_for_removal(
    db, store, hosts: PostgresHostPoolRepository
):
    import asyncpg

    await _enrol(store, "runner-a", hosts=hosts)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET removal_requested_at = NOW() WHERE host = 'runner-a'"
            )


async def test_a_forced_removal_ends_the_work_the_host_was_holding(
    store, hosts: PostgresHostPoolRepository
):
    """Revoking the credential also ends what the host's worker tokens open.

    Those tokens name the attempt, never the machine, so before the removal
    expired the leases a removed host kept heartbeating them — and with the
    lease alive its grant was renewed, the relay served it tools, and it could
    mint fresh residency grants. From the commit on, none of that succeeds,
    and the reaper's next pass (residencies, then attempts, then grants, as
    the loop runs them) ends the work and hands both grants over for
    revocation.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    fence = {"attempt_id": claim["attempt_id"], "lease_generation": claim["lease_generation"]}
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        **fence,
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=600,
    )
    assert residency is not None
    assert await store.grants.link_residency_grant(
        thread_id=job["thread_id"], generation=residency["generation"], grant_id="agr_resident"
    )
    assert await store.grants.record_attempt_grant(**fence, grant_id="agr_attempt")

    assert await hosts.remove_host(host="runner-a", force=True) == "ok"

    assert await store.attempt_commands.heartbeat(**fence, lease_ttl_seconds=600) == {"ok": False}
    assert await store.grants.list_renewable_grants() == []
    assert (
        await store.credential_queries.resolve_model_credential(job_id=job["id"], **fence) is None
    )
    assert await store.residency.resident_host(thread_id=job["thread_id"]) is None
    assert (
        await store.residency.establish_residency(
            thread_id=job["thread_id"],
            **fence,
            host="runner-a",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=600,
        )
        is None
    )

    lapsed = await store.residency.reap_expired_residencies()
    assert [row["grant_id"] for row in lapsed] == ["agr_resident"]
    reaped = await store.attempt_commands.reap_expired(max_attempts=3)
    assert [row["attempt_id"] for row in reaped] == [claim["attempt_id"]]
    assert [row["grant_id"] for row in await store.grants.take_revocable_grants()] == [
        "agr_attempt"
    ]
    assert (await hosts.get_host(host="runner-a"))["slots_used"] == 0


async def test_a_disabled_host_can_no_longer_extend_or_earn(
    store, hosts: PostgresHostPoolRepository
):
    """The live-host predicate on its own, without the revocation's expiry.

    ``disabled`` reached through the status table keeps the credential and
    leaves every lease where it was. Neither the lease nor the residency may
    be kept going from that machine, whatever its worker token still proves.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    fence = {"attempt_id": claim["attempt_id"], "lease_generation": claim["lease_generation"]}
    assert (await store.attempt_commands.heartbeat(**fence, lease_ttl_seconds=60))["ok"] is True

    assert await hosts.set_host_status(host="runner-a", status=HOST_DISABLED) == "ok"

    assert await store.attempt_commands.heartbeat(**fence, lease_ttl_seconds=60) == {"ok": False}
    assert (
        await store.residency.establish_residency(
            thread_id=job["thread_id"],
            **fence,
            host="runner-a",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )


async def test_forgetting_a_host_does_not_break_its_attempts(
    db, store, hosts: PostgresHostPoolRepository
):
    """The FK is ON DELETE SET NULL, so history survives its machine.

    A RESTRICT here would make #1158's housekeeping start failing on any
    machine that had ever run anything — which is every machine.
    """
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    await _settled(db, store, claim["id"])
    await store.attempt_commands.transition(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert await hosts.forget_runner_host(host="runner-a") == "deleted"
    assert await hosts.host_for_attempt(attempt_id=claim["attempt_id"]) is None


async def test_the_pin_and_the_pool_both_have_to_agree(store, hosts: PostgresHostPoolRepository):
    """They compose: the pin says which host, the pool says whether it can.

    #1158's switch is untouched by phase G, and a pinned host that has been
    drained still claims nothing — otherwise draining would silently unpin.
    """
    await _enrol(store, "runner-a", slots_total=2, hosts=hosts)
    await _enrol(store, "runner-b", slots_total=2, hosts=hosts)
    await _create_job(store)
    await hosts.set_active_runner_host(host="runner-a")

    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-b") is None
    await hosts.set_host_status(host="runner-a", status=HOST_DRAINING)
    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None


# ── Thread residency ───────────────────────────────────────────────────


async def _running_claim(store, host: str, **overrides) -> tuple[dict, dict]:
    """A job claimed and running on ``host`` — what earns a residency."""
    job = await _create_job(store, **overrides)
    claim = await _claim(store, worker_id=f"w-{host}", lease_ttl_seconds=60, host=host)
    assert claim is not None and claim["id"] == job["id"]
    return job, claim


async def _resident_chat(
    store, host: str, hosts: PostgresHostPoolRepository
) -> tuple[dict, dict, dict]:
    """A running claim on ``host`` with the retained membership a residency lives on."""
    job, claim = await _running_claim(store, host)
    await hosts.record_host_heartbeat(
        host=host,
        capabilities=["shared-sandbox-v1", "native-fork-at-anchor-v1"],
        sandbox_image_digest="sha256:shared-image",
    )
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=host,
        sandbox_id=f"asbx_{job['id']}",
        container_name=f"chat-{job['id']}",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    assert sandbox is not None
    return job, claim, sandbox


async def _reclaim_resident_chat(
    store,
    job: dict,
    claim: dict,
    sandbox: dict,
    *,
    host: str,
    worker_id: str = "w2",
) -> tuple[dict, dict]:
    """Cleanly hand one retained activation to a successor attempt."""
    abandoned = await store.sandbox.abandon_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        lost=False,
        detail="retry before prompt admission",
        idle_timeout_seconds=1800,
    )
    assert abandoned is not None
    assert await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )
    reclaimed = await _claim(store, worker_id=worker_id, lease_ttl_seconds=60, host=host)
    assert reclaimed is not None
    reserved = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=reclaimed["attempt_id"],
        lease_generation=reclaimed["lease_generation"],
        host=host,
        sandbox_id=sandbox["sandbox_id"],
        container_name=sandbox["container_name"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        sandbox_image_digest=sandbox["sandbox_image_digest"],
        execution_profile=job["execution_profile"],
    )
    assert reserved is not None
    return reclaimed, reserved


async def test_residency_is_earned_by_the_running_attempt_only(db, store, hosts):
    """Review finding 4: the gate shares the write's transaction and checks
    the lease clock — expired, finished, or another host's attempt earns
    nothing."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)

    # Another machine, even while the attempt runs: nothing.
    assert (
        await store.residency.establish_residency(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="runner-b",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )
    # The running host: earned.
    row = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert row is not None and row["generation"] == 1

    # A finished attempt: nothing — even before any reaper pass.
    await _settled(db, store, job["id"])
    await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (
        await store.residency.establish_residency(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="runner-a",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )


async def test_an_expired_lease_earns_no_residency(store, hosts):
    """Running in name only: the reaper has not passed, the clock has."""
    await _enrol(store, "runner-a", hosts=hosts)
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=-1, host="runner-a")
    assert claim is not None

    assert (
        await store.residency.establish_residency(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="runner-a",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )


async def test_lock_wait_cannot_outlive_the_attempt_lease(db, store, hosts):
    """Wall-clock expiry is rechecked after the earn gate waits on a row lock."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    establish = None

    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.execute(
            "UPDATE agent_attempts "
            "SET lease_expires_at = clock_timestamp() + INTERVAL '250 milliseconds' "
            "WHERE id = $1",
            claim["attempt_id"],
        )
        establish = asyncio.create_task(
            store.residency.establish_residency(
                thread_id=job["thread_id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                host="runner-a",
                runtime_id="claude-code",
                runtime_version=None,
                ttl_seconds=60,
                sandbox_id=sandbox["sandbox_id"],
            )
        )
        waiting = False
        async with db.acquire() as observer:
            for _ in range(100):
                waiting = await observer.fetchval(
                    """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_locks
                            WHERE NOT granted
                              AND $1 = ANY(pg_blocking_pids(pid))
                        )
                        """,
                    blocker_pid,
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
        if not waiting:
            establish.cancel()
            await asyncio.gather(establish, return_exceptions=True)
            pytest.fail("residency establish did not wait on the attempt row lock")
        await asyncio.sleep(0.35)

    assert establish is not None
    assert await asyncio.wait_for(establish, timeout=2) is None


async def test_a_takeover_bumps_the_generation_and_the_old_one_is_dead(store, hosts):
    """Contract tests 10 and 13: one winner, and late writers change nothing.

    A retained chat is never taken over by another machine — its writable
    layer lives on one host — so the rotation happens on the host that owns
    it, and the dead generation stays dead there.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    first = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert (first["generation"], first["status"]) == (1, "hot")

    # The attempt is released and reclaimed on the owning host, which
    # rotates the residency fence.
    reclaim, _sandbox = await _reclaim_resident_chat(store, job, claim, sandbox, host="runner-a")
    second = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=reclaim["attempt_id"],
        lease_generation=reclaim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert second["generation"] == 2

    live = await store.residency.resident_host(thread_id=job["thread_id"])
    assert (live["host"], live["generation"]) == ("runner-a", 2)
    # The previous generation can neither re-promote nor end its successor.
    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        is None
    )
    assert (
        await store.residency.release_residency(
            thread_id=job["thread_id"], generation=1, host="runner-a"
        )
        is None
    )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=2, ttl_seconds=60, host="runner-a"
        )
        is not None
    )


async def test_stale_attempt_cannot_rotate_the_current_residency(store, hosts):
    """A delayed same-host attempt cannot revoke its successor's grant."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, stale, sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    current, current_sandbox = await _reclaim_resident_chat(
        store, job, stale, sandbox, host="runner-a"
    )
    successor = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=current["attempt_id"],
        lease_generation=current["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
        sandbox_id=current_sandbox["sandbox_id"],
    )
    assert successor is not None
    assert await store.grants.link_residency_grant(
        thread_id=job["thread_id"],
        generation=successor["generation"],
        grant_id="agr-successor",
    )
    before = await store.residency.resident_host(thread_id=job["thread_id"])

    stale_result = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=stale["attempt_id"],
        lease_generation=stale["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
        sandbox_id=sandbox["sandbox_id"],
    )
    after = await store.residency.resident_host(thread_id=job["thread_id"])

    assert stale_result is None
    assert before is not None and after is not None
    assert (before["generation"], before["status"], before["grant_id"]) == (
        successor["generation"],
        "hot",
        "agr-successor",
    )
    assert (after["generation"], after["status"], after["grant_id"]) == (
        before["generation"],
        before["status"],
        before["grant_id"],
    )


async def test_residency_writes_are_scoped_to_the_holding_host(store, hosts):
    """A guessed (thread, generation) from another machine changes nothing."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    row = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    generation = row["generation"]

    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=generation, ttl_seconds=60, host="runner-b"
        )
        is None
    )
    assert (
        await store.residency.release_residency(
            thread_id=job["thread_id"], generation=generation, host="runner-b"
        )
        is None
    )
    assert (
        await store.residency.release_residency(
            thread_id=job["thread_id"], generation=generation, host="runner-a"
        )
        is not None
    )


async def test_an_expired_residency_cannot_be_revived_by_a_late_heartbeat(db, store, hosts):
    """The clock, not the reaper's next pass, removes heartbeat authority."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    row = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    await _expire_residency(db, thread_id=job["thread_id"])

    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"],
            generation=row["generation"],
            ttl_seconds=60,
            host="runner-a",
        )
        is None
    )
    assert await store.residency.resident_host(thread_id=job["thread_id"]) is None
    assert [item["thread_id"] for item in await store.residency.reap_expired_residencies()] == [
        job["thread_id"]
    ]


async def test_an_expired_residency_rejects_and_revokes_a_late_grant(db, store, hosts):
    """A mint that loses the clock race leaves neither a link nor a token."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    row = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    await _expire_residency(db, thread_id=job["thread_id"])
    grants = SimpleNamespace(
        mint=AsyncMock(return_value={"grant_id": "agr-late", "token": "tok-late"}),
        revoke=AsyncMock(return_value=True),
    )

    credential = await residency._mint_residency_grant(store.grants, grants, row=row)

    assert credential is None
    grants.revoke.assert_awaited_once_with("agr-late")
    assert [item["grant_id"] for item in await store.residency.reap_expired_residencies()] == [None]


async def test_the_residency_reaper_leaves_an_unexpired_hot_lease_alone(store, hosts):
    """The reaper is the expired complement, never a blanket hot-row revoke."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    live = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert live is not None

    assert await store.residency.reap_expired_residencies() == []
    current = await store.residency.resident_host(thread_id=job["thread_id"])
    assert current is not None
    assert current["generation"] == live["generation"]


async def test_the_reaper_lapses_expired_residencies_without_resetting_generations(store, hosts):
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=-1,
    )

    lapsed = await store.residency.reap_expired_residencies()

    assert [(row["thread_id"], row["generation"]) for row in lapsed] == [(job["thread_id"], 1)]
    assert await store.residency.resident_host(thread_id=job["thread_id"]) is None
    # Re-establishing continues the count — a dead generation stays dead.
    revived = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert revived["generation"] == 2


async def test_establish_returns_what_the_grant_mint_needs(store, hosts):
    """Retained-session design §6: mint a thread-attributed grant from exactly
    what the earn-gate already proved — the establishing claim's user, model
    and attempt — so the row must carry all three."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)

    row = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert row["user_id"] == "u1"
    assert row["model"] == "glm-5.1"
    assert row["establishing_job_id"] == job["id"]
    assert row["establishing_attempt_id"] == claim["attempt_id"]
    assert row["previous_grant_id"] is None


async def test_the_residency_grant_link_is_generation_scoped(store, hosts):
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )

    # A link named for a generation that does not hold the row is refused —
    # the one way it happens is a takeover between mint and link, and the
    # caller must revoke the orphan grant rather than hand it over.
    assert (
        await store.grants.link_residency_grant(
            thread_id=job["thread_id"], generation=2, grant_id="agr-orphan"
        )
        is False
    )
    assert (
        await store.grants.link_residency_grant(
            thread_id=job["thread_id"], generation=1, grant_id="agr-1"
        )
        is True
    )


async def test_beats_and_release_carry_the_grant_they_keep_alive(store, hosts):
    """The beat that extends the lease renews the grant; the release that ends
    it revokes the grant — so both hand their caller the grant id, and an
    unlinked residency answers with the empty string, not a loss."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )

    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        == ""
    )
    await store.grants.link_residency_grant(
        thread_id=job["thread_id"], generation=1, grant_id="agr-1"
    )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        == "agr-1"
    )
    assert (
        await store.residency.release_residency(
            thread_id=job["thread_id"], generation=1, host="runner-a"
        )
        == "agr-1"
    )


async def test_a_takeover_surfaces_the_predecessors_grant_for_revocation(store, hosts):
    """A takeover is a credential rotation: the new establish must name the
    old generation's grant so the endpoint can kill it now, not at TTL."""
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    await store.grants.link_residency_grant(
        thread_id=job["thread_id"], generation=1, grant_id="agr-old"
    )

    reclaim, _sandbox = await _reclaim_resident_chat(store, job, claim, sandbox, host="runner-a")
    second = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=reclaim["attempt_id"],
        lease_generation=reclaim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert second["generation"] == 2
    assert second["previous_grant_id"] == "agr-old"


async def test_reaped_residencies_carry_their_grant_id(db, store, hosts):
    """The reaper revokes what the lapse orphans — it needs the id to do it.

    The grant is linked while the lease is live. Once that lease is in the
    past, a further link is refused and cannot replace the id the reaper
    returns.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert (
        await store.grants.link_residency_grant(
            thread_id=job["thread_id"], generation=1, grant_id="agr-lapsed"
        )
        is True
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships "
            "SET residency_lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    assert (
        await store.grants.link_residency_grant(
            thread_id=job["thread_id"], generation=1, grant_id="agr-after-expiry"
        )
        is False
    )
    await _expire_residency(db, thread_id=job["thread_id"])

    lapsed = await store.residency.reap_expired_residencies()
    assert [(row["thread_id"], row["grant_id"]) for row in lapsed] == [
        (job["thread_id"], "agr-lapsed")
    ]


async def test_a_live_lease_heartbeats_and_links_and_an_expired_lease_does_not(db, store, hosts):
    """The clock ends a generation's authority before the reaper runs.

    A live lease still heartbeats and accepts its grant. The same generation,
    once the lease is in the past and the row is still hot, cannot extend
    itself or take another grant. Reaping and an explicit release of that
    expired row stay available.
    """
    await _enrol(store, "runner-a", hosts=hosts)
    job, claim, _sandbox = await _resident_chat(store, "runner-a", hosts=hosts)
    await store.residency.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )

    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        == ""
    )
    assert (
        await store.grants.link_residency_grant(
            thread_id=job["thread_id"], generation=1, grant_id="agr-live"
        )
        is True
    )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        == "agr-live"
    )
    assert (await store.residency.resident_host(thread_id=job["thread_id"]))["generation"] == 1

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships "
            "SET residency_lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE thread_id = $1",
            job["thread_id"],
        )

    assert await store.residency.resident_host(thread_id=job["thread_id"]) is None
    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        is None
    )
    assert (
        await store.grants.link_residency_grant(
            thread_id=job["thread_id"], generation=1, grant_id="agr-late"
        )
        is False
    )
    # The late beat did not push the lease forward, so the reaper still owns it.
    assert await store.residency.resident_host(thread_id=job["thread_id"]) is None
    lapsed = await store.residency.reap_expired_residencies()
    assert [(row["thread_id"], row["generation"], row["grant_id"]) for row in lapsed] == [
        (job["thread_id"], 1, "agr-live")
    ]

    expired_job, expired_claim, _expired_sandbox = await _resident_chat(
        store, "runner-a", hosts=hosts
    )
    await store.residency.establish_residency(
        thread_id=expired_job["thread_id"],
        host="runner-a",
        attempt_id=expired_claim["attempt_id"],
        lease_generation=expired_claim["lease_generation"],
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=-1,
    )
    assert await store.residency.resident_host(thread_id=expired_job["thread_id"]) is None
    assert (
        await store.residency.heartbeat_residency(
            thread_id=expired_job["thread_id"],
            generation=1,
            ttl_seconds=60,
            host="runner-a",
        )
        is None
    )
    assert (
        await store.grants.link_residency_grant(
            thread_id=expired_job["thread_id"], generation=1, grant_id="agr-born-expired"
        )
        is False
    )
    assert (
        await store.residency.release_residency(
            thread_id=expired_job["thread_id"], generation=1, host="runner-a"
        )
        == ""
    )
    assert await store.residency.reap_expired_residencies() == []


async def test_a_new_chat_without_a_group_is_claimable_by_an_enrolled_host(store, hosts):
    await _enrol(store, "runner-a", hosts=hosts)
    job = await _create_job(store)

    claimed = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claimed is not None and claimed["id"] == job["id"]


async def test_each_claim_keeps_its_own_image_stamp_across_a_rolling_upgrade(store, hosts):
    """A later replica image cannot rewrite an earlier attempt's stamp."""
    await _enrol(store, "runner-a", hosts=hosts)
    await _create_job(store)
    await _create_job(store, task_prompt="second queued chat")
    claim_one = await _claim(
        store,
        worker_id="replica-v1",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("cred-runner-a"),
        claim_attestation=_shared_attestation(sandbox_image_digest="sandbox@sha256:image-v1"),
    )
    assert claim_one is not None
    assert claim_one["runtime_version"] == "2.1.220"
    assert claim_one["sandbox_image_digest"] == "sandbox@sha256:image-v1"

    claim_two = await _claim(
        store,
        worker_id="replica-v2",
        lease_ttl_seconds=60,
        host="runner-a",
        host_credential_hash=hash_secret("cred-runner-a"),
        claim_attestation=_shared_attestation(sandbox_image_digest="sandbox@sha256:image-v2"),
    )
    assert claim_two is not None

    assert claim_two["sandbox_image_digest"] == "sandbox@sha256:image-v2"
    assert claim_one["sandbox_image_digest"] == "sandbox@sha256:image-v1"


# ── Choosing which version the pool runs ─────────────────────────────────


#: The image these tests' host holds: default first, then the extra.
MENU = ["2.1.220", "2.1.235"]


async def _menu_host(
    store, host: str, *, menu: list[str] = MENU, hosts: PostgresHostPoolRepository
) -> None:
    """A host whose *heartbeat* advertises a default and its image's menu.

    That row is what the admin page renders and what ``set_runtime_version``
    validates a choice against. It is deliberately not what the claim reads —
    see ``_menu_attestation``.
    """
    await _enrol(store, host, hosts=hosts)
    await hosts.record_host_heartbeat(
        host=host,
        sandbox_runtime="",
        sandbox_image_digest="sha256:menu",
        runtime_versions={"claude-code": menu[0]},
        runtime_versions_available={"claude-code": menu},
    )


def _menu_attestation(menu: list[str] = MENU) -> dict:
    """What the claiming replica says about its *own* image.

    The claim believes this rather than the heartbeat row: a mutable row shared
    by every replica on a machine is not evidence about the process actually
    taking the job during a rolling deployment.
    """
    return _shared_attestation(runtime_versions_available={"claude-code": menu})


async def _menu_claim(store, worker: str, *, menu: list[str] = MENU):
    """Claim as a replica whose image holds ``menu``."""
    return await _claim(
        store,
        worker_id=worker,
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=_menu_attestation(menu),
    )


async def test_a_heartbeat_round_trips_the_menu_beside_the_default(
    store, hosts: PostgresHostPoolRepository
):
    """Two facts, two columns; a stampless beat erases neither."""
    await _menu_host(store, "runner-a", hosts=hosts)

    row = await hosts.record_host_heartbeat(host="runner-a")

    assert row["runtime_versions"] == {"claude-code": "2.1.220"}
    assert row["runtime_versions_available"] == {"claude-code": ["2.1.220", "2.1.235"]}


async def test_a_selection_reaches_the_attempt_the_claim_stamps(
    store, hosts: PostgresHostPoolRepository
):
    """The feature, end to end in SQL: admin picks, the next attempt records it."""
    await _menu_host(store, "runner-a", hosts=hosts)
    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")
    await _create_job(store)  # runtime claude-code

    claim = await _menu_claim(store, "w1")

    assert claim is not None
    assert claim["runtime_version"] == "2.1.235"


async def test_a_version_no_host_offers_is_refused(store, hosts: PostgresHostPoolRepository):
    """Refused rather than stored: the claim would fall back and say nothing."""
    await _menu_host(store, "runner-a", menu=["2.1.220"], hosts=hosts)

    assert not await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")

    _, _, selected, _ = await hosts.runner_pool()
    assert selected == {}


async def test_clearing_a_selection_returns_to_the_host_default(
    store, hosts: PostgresHostPoolRepository
):
    await _menu_host(store, "runner-a", hosts=hosts)
    await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")

    assert await hosts.set_runtime_version(runtime="claude-code", version=None)
    await _create_job(store)
    claim = await _menu_claim(store, "w1")

    assert claim is not None
    assert claim["runtime_version"] == "2.1.220"


async def test_a_selection_the_image_lost_falls_back_instead_of_parking(
    store, hosts: PostgresHostPoolRepository
):
    """The drift the fallback exists for, reachable in exactly this order.

    A selection is only accepted while some host offers it — but the image can
    be rebuilt with a shorter menu afterwards, and then the stored choice names
    a version nothing installs. Refusing the claim there would park the queue
    on a configuration change nobody would connect to it; running the
    default is the smaller wrong, and the claim logs which it did.
    """
    await _menu_host(store, "runner-a", hosts=hosts)
    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")

    # The rebuild: same host, same name, an image that no longer holds 2.1.235.
    # The claiming replica says so itself, which is the evidence that counts —
    # its heartbeat may not have caught up mid-rollout.
    await _create_job(store)

    claim = await _menu_claim(store, "w1", menu=["2.1.220"])

    assert claim is not None, "an unservable selection must not park the queue"
    assert claim["runtime_version"] == "2.1.220"


async def test_physical_server_grouping_preserves_deployment_identity_and_labels(
    store, hosts: PostgresHostPoolRepository
):
    """Display metadata cannot merge credentials, sandbox managers, capacity or placement."""
    for name in ("gpu-box", "gpu-box-kata"):
        await hosts.register_host(
            host=name,
            credential_hash=hash_secret(name),
            sandbox_manager_url=f"http://{name}:8092",
            slots_total=3,
            labels={"zone": "test"},
        )
        await hosts.record_host_heartbeat(host=name, physical_host="gpu-box")
    await hosts.set_active_runner_host(host="gpu-box-kata")
    await hosts.set_host_status(host="gpu-box-kata", status=HOST_DRAINING)
    # Old replicas omitting the new field must not erase it.
    await hosts.record_host_heartbeat(host="gpu-box-kata")
    rows, active, _, _ = await hosts.runner_pool()
    assert active == "gpu-box-kata"
    assert len(rows) == 2
    for row in rows:
        assert row["labels"] == {"zone": "test", "physical_host": "gpu-box"}
        assert row["sandbox_manager_url"] == f"http://{row['host']}:8092"
        assert row["slots_total"] == 3
        assert row["status"] == (HOST_DRAINING if row["host"] == active else HOST_ONLINE)
    assert (
        await hosts.authenticate_host(host="gpu-box", credential_hash=hash_secret("gpu-box-kata"))
        is None
    )


# ── agent updates from the admin Settings page ───────────────────────────


async def _offering(
    hosts: PostgresHostPoolRepository, host: str, menu: list[str], reports: list | None = None
):
    await hosts.record_host_heartbeat(
        host=host,
        runtime_versions={"claude-code": "2.1.220"},
        runtime_versions_available={"claude-code": menu},
        runtime_downloads=reports,
    )


async def _age_record(db, runtime: str, before: str) -> None:
    """Pretend a record was made long ago, so keeping its cutoff is visible."""
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_policy "
            "SET runtime_upgrades = jsonb_set(runtime_upgrades, ARRAY[$1, 'before'], to_jsonb($2::text)) "
            "WHERE id",
            runtime,
            before,
        )


async def test_an_update_waits_for_every_online_host_then_switches_new_chats(
    store, hosts: PostgresHostPoolRepository
):
    """The last host to hold the version is the one whose beat switches."""
    await _enrol(store, "runner-a", hosts=hosts)
    await _enrol(store, "runner-b", hosts=hosts)
    # A draining host is not given new chats, so it is not waited for.
    await _enrol(store, "runner-c", hosts=hosts)
    await hosts.set_host_status(host="runner-c", status=HOST_DRAINING)

    target = await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    assert target["previous_version"] is None
    assert target["before"].endswith("Z")
    assert await hosts.runtime_download_targets() == [
        {
            "runtime": "claude-code",
            "version": "2.1.300",
            "before": target["before"],
            "nonce": target["nonce"],
        }
    ]

    await _offering(hosts, "runner-a", ["2.1.220", "2.1.300"])
    await _offering(hosts, "runner-b", ["2.1.220"])
    assert await hosts.activate_ready_runtime_upgrades() == []
    _, _, selected, _ = await hosts.runner_pool()
    assert selected == {}

    await _offering(hosts, "runner-b", ["2.1.220", "2.1.300"])
    assert await hosts.activate_ready_runtime_upgrades() == ["claude-code"]
    assert await hosts.activate_ready_runtime_upgrades() == []
    _, _, selected, _ = await hosts.runner_pool()
    assert selected == {"claude-code": "2.1.300"}

    upgrades, selection, rows = await hosts.runtime_upgrade_snapshot()
    assert upgrades["claude-code"]["activated_at"]
    assert selection == {"claude-code": "2.1.300"}
    assert [row["host"] for row in rows] == ["runner-a", "runner-b", "runner-c"]
    # Done targets stay on the wire: a host joining later still downloads them.
    assert [t["version"] for t in await hosts.runtime_download_targets()] == ["2.1.300"]


async def test_with_no_host_online_nothing_switches(store, hosts: PostgresHostPoolRepository):
    await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    assert await hosts.activate_ready_runtime_upgrades() == []


async def test_a_heartbeat_stores_its_download_reports(store, hosts: PostgresHostPoolRepository):
    await _enrol(store, "runner-a", hosts=hosts)
    report = {
        "runtime": "claude-code",
        "version": "2.1.300",
        "nonce": "n" * 32,
        "state": "downloading",
        "progress": 55,
        "error": None,
    }
    held = {**report, "version": "2.1.235", "state": "ready", "progress": 100}

    await _offering(hosts, "runner-a", ["2.1.220"], [report, held])
    await hosts.record_host_heartbeat(host="runner-a")  # reports nothing: keeps it

    _, _, rows = await hosts.runtime_upgrade_snapshot()
    assert rows[0]["runtime_downloads"] == [report, held]


async def test_a_retry_changes_only_the_nonce(db, store, hosts: PostgresHostPoolRepository):
    """Same cutoff, same rollback, same switch: only the request to try again is new."""
    await _menu_host(store, "runner-a", hosts=hosts)  # offers 2.1.220 and 2.1.235
    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")

    first = await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    await _age_record(db, "claude-code", "2026-01-01T00:00:00Z")
    retried = await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    assert retried["nonce"] != first["nonce"]
    # Hosts that already hold it keep the tree they installed; a host retrying
    # must resolve the same one.
    assert retried["before"] == "2026-01-01T00:00:00Z"
    assert retried["previous_version"] == "2.1.235"

    await _offering(hosts, "runner-a", ["2.1.220", "2.1.235", "2.1.300"])
    assert await hosts.activate_ready_runtime_upgrades() == ["claude-code"]
    switched, _, _ = await hosts.runtime_upgrade_snapshot()

    # A host that joined later failed, and the admin pressed Try again on a
    # done update: it must not become its own rollback, nor switch again.
    again = await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    assert again["nonce"] != retried["nonce"]
    assert again["previous_version"] == "2.1.235"
    assert again["before"] == "2026-01-01T00:00:00Z"
    assert again["activated_at"] == switched["claude-code"]["activated_at"]
    _, _, selected, _ = await hosts.runner_pool()
    assert selected == {"claude-code": "2.1.300"}


async def test_until_an_update_switches_hosts_also_hold_the_version_in_use(
    db, store, hosts: PostgresHostPoolRepository
):
    """A host joining mid-update, or after it failed, must run what the others run."""
    await _menu_host(store, "runner-a", menu=["2.1.220", "2.1.235"], hosts=hosts)
    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")
    await _age_record(db, "claude-code", "2026-01-01T00:00:00Z")
    in_use = (await hosts.runtime_download_targets())[0]
    assert (in_use["version"], in_use["before"]) == ("2.1.235", "2026-01-01T00:00:00Z")

    update = await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    assert update["previous_version"] == "2.1.235"
    assert await hosts.runtime_download_targets() == [
        in_use,
        {
            "runtime": "claude-code",
            "version": "2.1.300",
            "before": update["before"],
            "nonce": update["nonce"],
        },
    ]
    # Replacing a pending update keeps holding the same version in use.
    later = await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.301", requested_by="admin@test"
    )
    assert later["previous_version"] == "2.1.235"
    assert [t["version"] for t in await hosts.runtime_download_targets()] == ["2.1.235", "2.1.301"]

    await _offering(hosts, "runner-a", ["2.1.220", "2.1.235", "2.1.301"])
    assert await hosts.activate_ready_runtime_upgrades() == ["claude-code"]
    # Switched: only the update is sent; hosts keep their copy for a rollback.
    assert [t["version"] for t in await hosts.runtime_download_targets()] == ["2.1.301"]


async def test_rolling_back_keeps_the_version_on_every_host(
    db, store, hosts: PostgresHostPoolRepository
):
    """A host joining after a rollback downloads the rolled-back version too."""
    await _menu_host(store, "runner-a", menu=["2.1.220", "2.1.235"], hosts=hosts)
    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")
    await _age_record(db, "claude-code", "2026-01-01T00:00:00Z")
    await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )
    await _offering(hosts, "runner-a", ["2.1.220", "2.1.235", "2.1.300"])
    assert await hosts.activate_ready_runtime_upgrades() == ["claude-code"]

    # What Roll back sends: the version the update replaced.
    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")

    (target,) = await hosts.runtime_download_targets()
    # With the cutoff it was first downloaded with, so a new host installs
    # the tree every other host holds.
    assert (target["version"], target["before"]) == ("2.1.235", "2026-01-01T00:00:00Z")
    upgrades, selection, _ = await hosts.runtime_upgrade_snapshot()
    assert selection == {"claude-code": "2.1.235"}
    assert upgrades["claude-code"]["kind"] == "selection"
    assert upgrades["claude-code"]["activated_at"]
    # An update finishing late cannot switch new chats away from the choice.
    assert await hosts.activate_ready_runtime_upgrades() == []


async def test_a_hand_picked_version_replaces_the_update_and_the_default_clears_it(
    store, hosts: PostgresHostPoolRepository
):
    await _menu_host(store, "runner-a", hosts=hosts)  # offers 2.1.220 and 2.1.235
    await hosts.request_runtime_upgrade(
        runtime="claude-code", version="2.1.300", requested_by="admin@test"
    )

    assert await hosts.set_runtime_version(runtime="claude-code", version="2.1.235")
    assert [t["version"] for t in await hosts.runtime_download_targets()] == ["2.1.235"]
    await _offering(hosts, "runner-a", ["2.1.220", "2.1.235", "2.1.300"])
    assert await hosts.activate_ready_runtime_upgrades() == []
    _, _, selected, _ = await hosts.runner_pool()
    assert selected == {"claude-code": "2.1.235"}

    # The image default is on every host: nothing left to hand out.
    assert await hosts.set_runtime_version(runtime="claude-code", version=None)
    assert await hosts.runtime_download_targets() == []
    upgrades, selection, _ = await hosts.runtime_upgrade_snapshot()
    assert (upgrades, selection) == ({}, {})
