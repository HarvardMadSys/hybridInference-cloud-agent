"""The host pool against a real Postgres: capacity, capability, and the reaper.

Everything here needs the database to mean anything. A slot counter that never
goes negative is a statement about two transactions racing; an atomic
single-use token is a statement about an UPDATE's predicate; "capabilities
superset" is a jsonb containment operator. None of those can be tested against
a fake without testing the fake.

The fixture applies Alembic — including `0002_host_pool`, `0003_attempt_host`
and `0004_enroll_token_host` — exactly as a deploy does, so a migration that
produces a nearly-right schema fails here rather than at cutover.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

from cloud_agent.host_pool import (
    HOST_DISABLED,
    HOST_DRAINING,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    hash_secret,
)
from cloud_agent.storage.store import AgentJobStore
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


@pytest_asyncio.fixture
async def store():
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
        yield AgentJobStore(pool)
    finally:
        await _reset()
        await pool.close()


async def _create_job(store: AgentJobStore, **overrides) -> dict:
    params = {
        "user_id": "u1",
        "repo": "acme/widgets",
        "task_prompt": "do the thing",
        "runtime": "claude-code",
        "model": "glm-5.1",
    }
    params.update(overrides)
    return await store.create_job(**params)


async def _settled(store: AgentJobStore, job_id: str) -> None:
    """Stand in for the runner's settlement: a success carries its native ordinal.

    The runner settles the shared workspace before it reports success and the
    settlement assigns ``native_turn_no``; the schema refuses a succeeded row
    without one. Tests that skip the sandbox protocol and finish a job
    directly prepare that fact here, instead of the store guessing it.
    """
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET native_turn_no = turn_no "
            "WHERE id = $1 AND native_turn_no IS NULL",
            job_id,
        )


async def _enrol(store: AgentJobStore, host: str, **overrides) -> dict:
    """Register a host and put it online, the way a real boot does."""
    row = await store.register_host(
        host=host, credential_hash=hash_secret(f"cred-{host}"), **overrides
    )
    assert row["status"] == HOST_ENROLLING
    return await store.record_host_heartbeat(host=host)


def _kata_attestation(**overrides) -> dict:
    stamp = {
        "sandbox_runtime": "io.containerd.kata.v2",
        "sandbox_image_digest": "sandbox@sha256:claim",
        "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
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
        "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
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
    store: AgentJobStore,
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
    return await store.claim_job(
        worker_id=worker_id,
        lease_ttl_seconds=lease_ttl_seconds,
        host=host,
        host_credential_hash=credential,
        claim_attestation=attestation,
        expected_deployment_sha=expected_deployment_sha,
    )


# ── G2: enrollment tokens ──────────────────────────────────────────────


async def test_an_enrollment_token_is_spendable_exactly_once(store):
    """Same name both times: the second refusal must be replay, not the binding."""
    await store.issue_enroll_token(token_hash=hash_secret("t"), host="runner-a", ttl_seconds=900)

    first = await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")
    second = await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")

    assert (first, second) == (True, False)


async def test_two_machines_racing_one_token_produce_one_winner(store):
    """The single-use property is the UPDATE's predicate, not a read-then-write.

    A token pasted into two consoles at once is exactly how a fleet gets
    enrolled from one credential, and a check-then-set would let both through.
    Every racer claims the name the token names, so what is under test is the
    predicate on ``consumed_at`` rather than the one on ``host``.
    """
    await store.issue_enroll_token(token_hash=hash_secret("t"), host="runner-a", ttl_seconds=900)

    results = await asyncio.gather(
        *(
            store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")
            for _ in range(8)
        )
    )

    assert sum(results) == 1


async def test_an_expired_token_cannot_be_spent(store):
    await store.issue_enroll_token(token_hash=hash_secret("t"), host="runner-a", ttl_seconds=-1)

    assert not await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")


async def test_a_token_is_spendable_only_under_the_name_it_names(store):
    """The name is the token's, not the registering machine's.

    ``register_host`` upserts, so spending a token under a live host's name
    revokes that host's credential and resets its row — a typo during a setup
    took a working machine out of the pool. The name therefore comes from the
    token an admin issued.
    """
    await store.issue_enroll_token(token_hash=hash_secret("t"), host="runner-b", ttl_seconds=900)

    assert not await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")


async def test_a_mismatched_name_does_not_spend_the_token(store):
    """A typo is retried, not re-issued: the name is a predicate, not a check."""
    await store.issue_enroll_token(token_hash=hash_secret("t"), host="runner-b", ttl_seconds=900)
    await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")

    assert await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-b")


async def test_a_typo_cannot_revoke_a_live_hosts_credential(store):
    """The eviction the binding removes, end to end against the real schema."""
    await _enrol(store, "runner-a", slots_total=4)
    await store.issue_enroll_token(token_hash=hash_secret("t"), host="runner-b", ttl_seconds=900)

    spent = await store.consume_enroll_token(token_hash=hash_secret("t"), host="runner-a")

    assert spent is False
    survivor = await store.get_host(host="runner-a")
    assert survivor["status"] == HOST_ONLINE
    assert survivor["enrolled"] is True
    assert await store.authenticate_host(
        host="runner-a", credential_hash=hash_secret("cred-runner-a")
    )


# ── G2/G3: registration, authentication, heartbeat ─────────────────────


async def test_registration_stores_what_the_host_declared(store):
    await store.register_host(
        host="runner-a",
        credential_hash=hash_secret("cred"),
        capabilities=["runc", "kata", "kata"],
        slots_total=3,
        broker_url="http://broker-a:8092",
        runner_version="1.2.3",
        labels={"region": "cambridge"},
    )

    row = await store.get_host(host="runner-a")
    assert row["status"] == HOST_ENROLLING
    assert row["capabilities"] == ["kata", "runc"]
    assert row["slots_total"] == 3
    assert row["broker_url"] == "http://broker-a:8092"
    assert row["labels"] == {"region": "cambridge"}
    assert row["enrolled"] is True


async def test_the_credential_hash_never_leaves_the_store(store):
    """Reading it back out would make the row as good as the credential."""
    await store.register_host(host="runner-a", credential_hash=hash_secret("cred"))

    assert "credential_hash" not in (await store.get_host(host="runner-a"))


async def test_only_the_current_credential_authenticates(store):
    await store.register_host(host="runner-a", credential_hash=hash_secret("first"))
    await store.register_host(host="runner-a", credential_hash=hash_secret("second"))

    assert (
        await store.authenticate_host(host="runner-a", credential_hash=hash_secret("first")) is None
    )
    assert await store.authenticate_host(host="runner-a", credential_hash=hash_secret("second"))


async def test_a_disabled_host_authenticates_nothing(store):
    await store.register_host(host="runner-a", credential_hash=hash_secret("cred"))
    await store.remove_host(host="runner-a")

    assert (
        await store.authenticate_host(host="runner-a", credential_hash=hash_secret("cred")) is None
    )


async def test_the_first_heartbeat_promotes_and_records(store):
    await store.register_host(host="runner-a", credential_hash=hash_secret("cred"))

    row = await store.record_host_heartbeat(host="runner-a", runner_version="9.9")

    assert row["status"] == HOST_ONLINE
    assert row["runner_version"] == "9.9"
    assert row["last_heartbeat_at"] is not None


async def test_a_heartbeat_does_not_resurrect_a_drained_or_disabled_host(store):
    await _enrol(store, "runner-a")
    await store.set_host_status(host="runner-a", status=HOST_DRAINING)

    assert (await store.record_host_heartbeat(host="runner-a"))["status"] == HOST_DRAINING


async def test_re_registration_recounts_slots_rather_than_zeroing_them(store):
    """A machine rebuilt mid-job must not be told it is empty.

    Zeroing would over-subscribe it while the old attempts still hold leases;
    counting is right whether the machine was rebuilt or merely re-registered.
    """
    await _enrol(store, "runner-a", slots_total=4)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    assert (await store.get_host(host="runner-a"))["slots_used"] == 1

    await store.register_host(host="runner-a", credential_hash=hash_secret("new"), slots_total=4)

    assert (await store.get_host(host="runner-a"))["slots_used"] == 1


# ── G3: the status machine and the reaper ──────────────────────────────


async def test_an_illegal_transition_is_refused_by_the_store(store):
    await _enrol(store, "runner-a")
    await store.remove_host(host="runner-a")

    assert await store.set_host_status(host="runner-a", status=HOST_ONLINE) == "invalid"
    assert (await store.get_host(host="runner-a"))["status"] == HOST_DISABLED


async def test_an_unknown_host_cannot_be_transitioned(store):
    assert await store.set_host_status(host="nope", status=HOST_DRAINING) == "unknown"


async def test_the_reaper_marks_a_silent_enrolled_host_offline(store):
    await _enrol(store, "runner-a")
    await _age_heartbeat(store, "runner-a", seconds=600)

    reaped = await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3)

    assert reaped == ["runner-a"]
    assert (await store.get_host(host="runner-a"))["status"] == HOST_OFFLINE


async def test_the_reaper_leaves_a_recently_seen_host_alone(store):
    await _enrol(store, "runner-a")

    assert await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == []


async def test_a_drained_host_stays_drained_when_it_goes_quiet_and_returns(store):
    """The operator's instruction must survive the machine's absence.

    This failed before: the reaper moved ``draining -> offline``, and the next
    heartbeat moved ``offline -> online``, because a machine returning from a
    partition is meant to come back without an operator. Composed, that made a
    drained host rejoin the claim pool by disappearing and coming back —
    quietly taking the work the operator had just decided it must not take.

    Drain is an instruction, not an observation. Nothing overwrites it.
    """
    await _enrol(store, "runner-a")
    await store.set_host_status(host="runner-a", status=HOST_DRAINING)
    await _age_heartbeat(store, "runner-a", seconds=600)

    assert await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == []
    assert (await store.get_host(host="runner-a"))["status"] == HOST_DRAINING

    # And the return does not promote it either.
    await store.record_host_heartbeat(host="runner-a")
    assert (await store.get_host(host="runner-a"))["status"] == HOST_DRAINING


async def test_a_host_that_registered_and_never_came_up_ages_out_of_enrolling(store):
    await store.register_host(host="runner-a", credential_hash=hash_secret("cred"))
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET first_seen_at = NOW() - INTERVAL '1 hour' "
            "WHERE host = 'runner-a'"
        )

    assert await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == ["runner-a"]


async def test_a_disabled_host_is_not_reaped_again(store):
    """Nothing to say: it is already out, and re-reporting it is noise."""
    await _enrol(store, "runner-a")
    await store.remove_host(host="runner-a")
    await _age_heartbeat(store, "runner-a", seconds=600)

    assert await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == []


async def _age_heartbeat(store: AgentJobStore, host: str, *, seconds: int) -> None:
    """Push a host's last heartbeat into the past, without sleeping."""
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts "
            "SET last_heartbeat_at = NOW() - make_interval(secs => $2) "
            "WHERE host = $1",
            host,
            seconds,
        )


# ── G4: host-aware claiming ────────────────────────────────────────────


async def test_a_claim_takes_a_slot_and_stamps_the_attempt(store):
    await _enrol(store, "runner-a", slots_total=2)
    await _create_job(store)

    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None
    assert (await store.get_host(host="runner-a"))["slots_used"] == 1
    assert (await store.host_for_attempt(attempt_id=claim["attempt_id"]))["host"] == "runner-a"


async def test_a_heartbeat_round_trips_the_version_stamp(store):
    """'' and {} are answers, and a stampless beat does not erase them."""
    await _enrol(store, "runner-a")
    row = await store.record_host_heartbeat(
        host="runner-a",
        sandbox_runtime="",
        sandbox_image_digest="sha256:abc",
        runtime_versions={},
    )
    assert row["sandbox_runtime"] == ""
    assert row["sandbox_image_digest"] == "sha256:abc"
    assert row["runtime_versions"] == {}

    row = await store.record_host_heartbeat(host="runner-a")

    assert row["sandbox_runtime"] == ""
    assert row["sandbox_image_digest"] == "sha256:abc"
    assert row["runtime_versions"] == {}


async def test_a_claim_stamps_its_replicas_attestation_not_the_host_heartbeat(store):
    """Rolling replicas sharing a host row are judged and stamped independently."""
    await _enrol(store, "runner-a")
    await store.record_host_heartbeat(
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
    async with store._pool.acquire() as conn:
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

    await store.record_host_heartbeat(
        host="runner-a",
        sandbox_runtime="io.containerd.kata.v2",
        sandbox_image_digest="sha256:newer-host-row",
        runtime_versions={"claude-code": "3.0.0"},
    )
    async with store._pool.acquire() as conn:
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
    await _enrol(store, "runner-b")
    await _create_job(store)
    trusted = await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-b")
    assert trusted is not None
    async with store._pool.acquire() as conn:
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
        _shared_attestation(shared_sandbox_protocol="v0"),
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
async def test_shared_host_row_cannot_authorize_a_mismatched_polling_replica(store, attestation):
    """One upgraded sibling's heartbeat is not another replica's claim proof."""
    await _enrol(store, "runner-a", capabilities=["shared-sandbox-v1"], slots_total=2)
    job = await _create_job(store, metadata={"requires": ["shared-sandbox-v1"]})

    claim = await _claim(
        store,
        worker_id="rolling-old-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=attestation,
    )

    assert claim is None
    assert (await store.get_job(job["id"]))["state"] == "queued"


async def test_exact_polling_replica_can_claim_shared_work(store):
    await _enrol(store, "runner-a", capabilities=["shared-sandbox-v1"], slots_total=2)
    job = await _create_job(store, metadata={"requires": ["shared-sandbox-v1"]})

    claim = await _claim(
        store,
        worker_id="rolling-new-replica",
        lease_ttl_seconds=60,
        host="runner-a",
        claim_attestation=_shared_attestation(),
    )

    assert claim is not None and claim["id"] == job["id"]


async def test_new_group_claim_requires_the_control_planes_exact_deployment(store):
    await _enrol(store, "runner-a", capabilities=["shared-sandbox-v1"], slots_total=2)
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


async def test_runc_replica_skips_secure_work_and_takes_the_next_trusted_job(store):
    await _enrol(store, "runner-a", capabilities=["kata"])
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
    assert (await store.get_job(secure["id"]))["state"] == "queued"


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
async def test_capability_labels_or_arbitrary_runtime_cannot_claim_secure_work(store, attestation):
    await _enrol(store, "runner-a", capabilities=["kata"])
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


async def test_shared_or_superseded_host_credential_cannot_claim_secure_work(store):
    await _enrol(store, "runner-a")
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


async def test_reenrollment_revokes_an_already_authenticated_claim_generation(store):
    await _enrol(store, "runner-a")
    old_hash = hash_secret("cred-runner-a")
    await store.register_host(host="runner-a", credential_hash=hash_secret("replacement"))
    await store.record_host_heartbeat(host="runner-a")
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


async def test_a_full_host_claims_nothing_more(store):
    await _enrol(store, "runner-a", slots_total=1)
    await _create_job(store)
    await _create_job(store)

    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    second = await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    assert first is not None
    assert second is None


async def test_the_job_a_full_host_could_not_take_is_still_queued(store):
    """Refusing must not consume the job, or capacity becomes data loss."""
    await _enrol(store, "runner-a", slots_total=1)
    job = await _create_job(store)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    states = {job["id"]: (await store.get_job(job["id"]))["state"]}
    assert "queued" in states.values() or states[job["id"]] == "running"
    async with store._pool.acquire() as conn:
        queued = await conn.fetchval("SELECT count(*) FROM agent_jobs WHERE state = 'queued'")
    assert queued == 1


async def test_an_unmetered_host_keeps_claiming(store):
    """Null slots_total is the pre-pool shape and must stay unlimited."""
    await _enrol(store, "runner-a")
    for _ in range(3):
        await _create_job(store)

    claims = [
        await _claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a")
        for n in range(3)
    ]

    assert all(claim is not None for claim in claims)
    assert (await store.get_host(host="runner-a"))["slots_used"] == 3


async def test_a_draining_host_receives_no_new_claims(store):
    await _enrol(store, "runner-a", slots_total=4)
    await _create_job(store)
    await store.set_host_status(host="runner-a", status=HOST_DRAINING)

    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None


@pytest.mark.parametrize("status", [HOST_ENROLLING, HOST_OFFLINE, HOST_DISABLED])
async def test_only_an_online_host_claims(store, status):
    await _enrol(store, "runner-a", slots_total=4)
    await _create_job(store)
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET status = $2 WHERE host = $1", "runner-a", status
        )

    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None


async def test_a_host_missing_a_required_capability_skips_that_job(store):
    """And takes the next one it *can* run, rather than stalling behind it."""
    await _enrol(store, "runner-a", slots_total=4, capabilities=["runc"])
    await _create_job(store, metadata={"requires": ["kata"]})
    runnable = await _create_job(store, task_prompt="the plain one")

    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None
    assert claim["id"] == runnable["id"]


async def test_a_host_declaring_a_superset_takes_the_job(store):
    await _enrol(store, "runner-a", slots_total=4, capabilities=["kata", "gpu", "runc"])
    job = await _create_job(store, metadata={"requires": ["kata", "gpu"]})

    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claim is not None
    assert claim["id"] == job["id"]


async def test_a_runner_without_an_enrolled_host_never_claims(store):
    """The new topology has no anonymous/single-host claim fallback."""
    await _create_job(store)

    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60) is None


async def test_finishing_a_job_gives_the_slot_back(store):
    await _enrol(store, "runner-a", slots_total=1)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    await _settled(store, claim["id"])
    await store.transition(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert (await store.get_host(host="runner-a"))["slots_used"] == 0


async def test_releasing_an_unstarted_claim_gives_the_slot_back(store):
    await _enrol(store, "runner-a", slots_total=1)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    await store.release_claim(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert (await store.get_host(host="runner-a"))["slots_used"] == 0


async def test_a_reaped_attempt_gives_the_slot_back(store):
    await _enrol(store, "runner-a", slots_total=1)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=-1, host="runner-a")

    await store.reap_expired(max_attempts=3)

    assert (await store.get_host(host="runner-a"))["slots_used"] == 0


async def test_a_double_terminal_transition_releases_one_slot_not_two(store):
    """The counter must survive a caller that retries a finish."""
    await _enrol(store, "runner-a", slots_total=2)
    await _create_job(store)
    await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    for _ in range(3):
        await _settled(store, first["id"])
        await store.transition(
            job_id=first["id"],
            attempt_id=first["attempt_id"],
            lease_generation=first["lease_generation"],
            from_states=("running",),
            to_state="succeeded",
        )

    assert (await store.get_host(host="runner-a"))["slots_used"] == 1


async def test_reaping_and_finishing_the_same_attempt_releases_one_slot(store):
    """Two paths out of ``running`` that can both be attempted for one attempt."""
    await _enrol(store, "runner-a", slots_total=2)
    await _create_job(store)
    await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=-1, host="runner-a")
    await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")

    await store.reap_expired(max_attempts=3)
    await _settled(store, first["id"])
    await store.transition(
        job_id=first["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.release_claim(
        job_id=first["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
    )

    assert (await store.get_host(host="runner-a"))["slots_used"] == 1


async def test_slots_never_go_negative_under_concurrent_release(store):
    """The invariant, hammered.

    A counter that drifts below zero makes a host look permanently
    over-committed and quietly stops it claiming, with nothing in the logs to
    say why. Guarded three times over — the release fires only on the UPDATE
    that actually closed the attempt, the decrement has a ``slots_used > 0``
    predicate, and the column carries a CHECK — so this asserts on the outcome
    of all three together.
    """
    await _enrol(store, "runner-a", slots_total=4)
    claims = []
    for _ in range(4):
        await _create_job(store)
        claims.append(await _claim(store, worker_id="w", lease_ttl_seconds=60, host="runner-a"))
    assert (await store.get_host(host="runner-a"))["slots_used"] == 4

    # Every exit path, several times each, all at once.
    for claim in claims:
        await _settled(store, claim["id"])
    await asyncio.gather(
        *[
            store.transition(
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
            store.release_claim(
                job_id=claim["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
            )
            for claim in claims
            for _ in range(3)
        ],
        *[store.reap_expired(max_attempts=3) for _ in range(3)],
    )

    assert (await store.get_host(host="runner-a"))["slots_used"] == 0


async def test_concurrent_claims_never_oversubscribe_a_host(store):
    """Capacity is decided by the UPDATE, not by the read that preceded it.

    Eight runners polling a two-slot host at once is the ordinary steady state
    of a replica set, and a check-then-increment hands out eight.
    """
    await _enrol(store, "runner-a", slots_total=2)
    for _ in range(8):
        await _create_job(store)

    claims = await asyncio.gather(
        *(_claim(store, worker_id=f"w{n}", lease_ttl_seconds=60, host="runner-a") for n in range(8))
    )

    granted = [claim for claim in claims if claim is not None]
    assert len(granted) == 2
    assert (await store.get_host(host="runner-a"))["slots_used"] == 2


async def test_the_release_helper_refuses_to_decrement_past_zero(store):
    """Tested directly, because nothing above can currently reach it twice.

    The job-level fence in ``transition`` already makes a second terminal
    transition impossible, so a test driving the public API cannot distinguish
    a guarded release from an unguarded one — and the guard would rot unnoticed
    until some future caller released a slot on a path the fence does not
    cover. Calling it straight is the only way to assert what it promises.
    """
    await _enrol(store, "runner-a", slots_total=2)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    async with store._pool.acquire() as conn:
        for _ in range(5):
            await store._release_host_slot(conn, "runner-a")

    assert (await store.get_host(host="runner-a"))["slots_used"] == 0


async def test_releasing_a_slot_for_no_host_is_a_no_op(store):
    """The hostless-runner path reaches this with ``None`` on every finish."""
    async with store._pool.acquire() as conn:
        await store._release_host_slot(conn, None)


async def test_the_schema_itself_refuses_a_negative_slot_count(store):
    """The last line of defence, asserted directly.

    If a future writer forgets the ``slots_used > 0`` predicate, this is what
    turns a silently-stuck host into a loud failure at the write that caused it.
    """
    import asyncpg

    await _enrol(store, "runner-a", slots_total=1)

    async with store._pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET slots_used = -1 WHERE host = 'runner-a'"
            )


async def test_the_schema_refuses_a_status_it_does_not_know(store):
    import asyncpg

    await _enrol(store, "runner-a")

    async with store._pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = 'sleepy' WHERE host = 'runner-a'"
            )


# ── G6 / #1158 interaction ─────────────────────────────────────────────


async def test_removing_keeps_the_row_and_forgetting_drops_it(store):
    await _enrol(store, "runner-a")

    assert await store.remove_host(host="runner-a") == "ok"
    assert await store.get_host(host="runner-a") is not None
    assert await store.forget_runner_host(host="runner-a") == "deleted"
    assert await store.get_host(host="runner-a") is None


async def test_removing_a_busy_host_is_refused_unless_forced(store):
    await _enrol(store, "runner-a", slots_total=2)
    await _create_job(store)
    await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert await store.remove_host(host="runner-a") == "busy"
    assert await store.remove_host(host="runner-a", force=True) == "ok"


async def test_forgetting_a_host_does_not_break_its_attempts(store):
    """The FK is ON DELETE SET NULL, so history survives its machine.

    A RESTRICT here would make #1158's housekeeping start failing on any
    machine that had ever run anything — which is every machine.
    """
    await _enrol(store, "runner-a", slots_total=2)
    await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")
    await _settled(store, claim["id"])
    await store.transition(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert await store.forget_runner_host(host="runner-a") == "deleted"
    assert await store.host_for_attempt(attempt_id=claim["attempt_id"]) is None


async def test_the_pin_and_the_pool_both_have_to_agree(store):
    """They compose: the pin says which host, the pool says whether it can.

    #1158's switch is untouched by phase G, and a pinned host that has been
    drained still claims nothing — otherwise draining would silently unpin.
    """
    await _enrol(store, "runner-a", slots_total=2)
    await _enrol(store, "runner-b", slots_total=2)
    await _create_job(store)
    await store.set_active_runner_host(host="runner-a")

    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-b") is None
    await store.set_host_status(host="runner-a", status=HOST_DRAINING)
    assert await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a") is None


# ── P3: thread residency (design §7) ───────────────────────────────────


async def _running_claim(store, host: str, **overrides) -> tuple[dict, dict]:
    """A job claimed and running on ``host`` — what earns a residency."""
    job = await _create_job(store, **overrides)
    claim = await _claim(store, worker_id=f"w-{host}", lease_ttl_seconds=60, host=host)
    assert claim is not None and claim["id"] == job["id"]
    return job, claim


async def _resident_chat(store, host: str) -> tuple[dict, dict, dict]:
    """A running claim on ``host`` with the retained membership a residency lives on."""
    job, claim = await _running_claim(store, host)
    await store.record_host_heartbeat(
        host=host,
        capabilities=["shared-sandbox-v1", "native-fork-at-anchor-v1"],
        sandbox_image_digest="sha256:shared-image",
    )
    sandbox = await store.reserve_thread_sandbox(
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


async def test_residency_is_earned_by_the_running_attempt_only(store):
    """Review finding 4: the gate shares the write's transaction and checks
    the lease clock — expired, finished, or another host's attempt earns
    nothing."""
    await _enrol(store, "runner-a")
    job, claim, _sandbox = await _resident_chat(store, "runner-a")

    # Another machine, even while the attempt runs: nothing.
    assert (
        await store.establish_residency(
            thread_id=job["thread_id"],
            host="runner-b",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )
    # The running host: earned.
    row = await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert row is not None and row["generation"] == 1

    # A finished attempt: nothing — even before any reaper pass.
    await _settled(store, job["id"])
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (
        await store.establish_residency(
            thread_id=job["thread_id"],
            host="runner-a",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )


async def test_an_expired_lease_earns_no_residency(store):
    """Running in name only: the reaper has not passed, the clock has."""
    await _enrol(store, "runner-a")
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=-1, host="runner-a")
    assert claim is not None

    assert (
        await store.establish_residency(
            thread_id=job["thread_id"],
            host="runner-a",
            runtime_id="claude-code",
            runtime_version=None,
            ttl_seconds=60,
        )
        is None
    )


async def test_a_takeover_bumps_the_generation_and_the_old_one_is_dead(store):
    """Contract tests 10 and 13: one winner, and late writers change nothing.

    A retained chat is never taken over by another machine — its writable
    layer lives on one host — so the rotation happens on the host that owns
    it, and the dead generation stays dead there.
    """
    await _enrol(store, "runner-a")
    job, claim, _sandbox = await _resident_chat(store, "runner-a")
    first = await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert (first["generation"], first["status"]) == (1, "hot")

    # The attempt is released and reclaimed on the owning host, which
    # rotates the residency fence.
    await store.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )
    reclaim = await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")
    assert reclaim is not None
    second = await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert second["generation"] == 2

    live = await store.resident_host(thread_id=job["thread_id"])
    assert (live["host"], live["generation"]) == ("runner-a", 2)
    # The previous generation can neither re-promote nor end its successor.
    assert (
        await store.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        is None
    )
    assert (
        await store.release_residency(thread_id=job["thread_id"], generation=1, host="runner-a")
        is None
    )
    assert (
        await store.heartbeat_residency(
            thread_id=job["thread_id"], generation=2, ttl_seconds=60, host="runner-a"
        )
        is not None
    )


async def test_residency_writes_are_scoped_to_the_holding_host(store):
    """A guessed (thread, generation) from another machine changes nothing."""
    await _enrol(store, "runner-a")
    job, _claim, _sandbox = await _resident_chat(store, "runner-a")
    row = await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    generation = row["generation"]

    assert (
        await store.heartbeat_residency(
            thread_id=job["thread_id"], generation=generation, ttl_seconds=60, host="runner-b"
        )
        is None
    )
    assert (
        await store.release_residency(
            thread_id=job["thread_id"], generation=generation, host="runner-b"
        )
        is None
    )
    assert (
        await store.release_residency(
            thread_id=job["thread_id"], generation=generation, host="runner-a"
        )
        is not None
    )


async def test_the_reaper_lapses_expired_residencies_without_resetting_generations(store):
    await _enrol(store, "runner-a")
    job, _claim, _sandbox = await _resident_chat(store, "runner-a")
    await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=-1,
    )

    lapsed = await store.reap_expired_residencies()

    assert [(row["thread_id"], row["generation"]) for row in lapsed] == [(job["thread_id"], 1)]
    assert await store.resident_host(thread_id=job["thread_id"]) is None
    # Re-establishing continues the count — a dead generation stays dead.
    revived = await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert revived["generation"] == 2


async def test_establish_returns_what_the_grant_mint_needs(store):
    """Retained-session design §6: mint a thread-attributed grant from exactly
    what the earn-gate already proved — the establishing claim's user, model
    and attempt — so the row must carry all three."""
    await _enrol(store, "runner-a")
    job, claim, _sandbox = await _resident_chat(store, "runner-a")

    row = await store.establish_residency(
        thread_id=job["thread_id"],
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


async def test_the_residency_grant_link_is_generation_scoped(store):
    await _enrol(store, "runner-a")
    job, _claim, _sandbox = await _resident_chat(store, "runner-a")
    await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )

    # A link named for a generation that does not hold the row is refused —
    # the one way it happens is a takeover between mint and link, and the
    # caller must revoke the orphan grant rather than hand it over.
    assert (
        await store.link_residency_grant(
            thread_id=job["thread_id"], generation=2, grant_id="agr-orphan"
        )
        is False
    )
    assert (
        await store.link_residency_grant(thread_id=job["thread_id"], generation=1, grant_id="agr-1")
        is True
    )


async def test_beats_and_release_carry_the_grant_they_keep_alive(store):
    """The beat that extends the lease renews the grant; the release that ends
    it revokes the grant — so both hand their caller the grant id, and an
    unlinked residency answers with the empty string, not a loss."""
    await _enrol(store, "runner-a")
    job, _claim, _sandbox = await _resident_chat(store, "runner-a")
    await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )

    assert (
        await store.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        == ""
    )
    await store.link_residency_grant(thread_id=job["thread_id"], generation=1, grant_id="agr-1")
    assert (
        await store.heartbeat_residency(
            thread_id=job["thread_id"], generation=1, ttl_seconds=60, host="runner-a"
        )
        == "agr-1"
    )
    assert (
        await store.release_residency(thread_id=job["thread_id"], generation=1, host="runner-a")
        == "agr-1"
    )


async def test_a_takeover_surfaces_the_predecessors_grant_for_revocation(store):
    """A takeover is a credential rotation: the new establish must name the
    old generation's grant so the endpoint can kill it now, not at TTL."""
    await _enrol(store, "runner-a")
    job, claim, _sandbox = await _resident_chat(store, "runner-a")
    await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    await store.link_residency_grant(thread_id=job["thread_id"], generation=1, grant_id="agr-old")

    await store.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60, host="runner-a")
    second = await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=60,
    )
    assert second["generation"] == 2
    assert second["previous_grant_id"] == "agr-old"


async def test_reaped_residencies_carry_their_grant_id(store):
    """The reaper revokes what the lapse orphans — it needs the id to do it."""
    await _enrol(store, "runner-a")
    job, _claim, _sandbox = await _resident_chat(store, "runner-a")
    await store.establish_residency(
        thread_id=job["thread_id"],
        host="runner-a",
        runtime_id="claude-code",
        runtime_version=None,
        ttl_seconds=-1,
    )
    await store.link_residency_grant(
        thread_id=job["thread_id"], generation=1, grant_id="agr-lapsed"
    )

    lapsed = await store.reap_expired_residencies()
    assert [(row["thread_id"], row["grant_id"]) for row in lapsed] == [
        (job["thread_id"], "agr-lapsed")
    ]


async def test_a_new_chat_without_a_group_is_claimable_by_an_enrolled_host(store):
    await _enrol(store, "runner-a")
    job = await _create_job(store)

    claimed = await _claim(store, worker_id="w1", lease_ttl_seconds=60, host="runner-a")

    assert claimed is not None and claimed["id"] == job["id"]


async def test_each_claim_keeps_its_own_image_stamp_across_a_rolling_upgrade(store):
    """A later replica image cannot rewrite an earlier attempt's stamp."""
    await _enrol(store, "runner-a")
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


async def _menu_host(store, host: str, *, menu: list[str] = MENU) -> None:
    """A host whose *heartbeat* advertises a default and its image's menu.

    That row is what the admin page renders and what ``set_runtime_version``
    validates a choice against. It is deliberately not what the claim reads —
    see ``_menu_attestation``.
    """
    await _enrol(store, host)
    await store.record_host_heartbeat(
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


async def test_a_heartbeat_round_trips_the_menu_beside_the_default(store):
    """Two facts, two columns; a stampless beat erases neither."""
    await _menu_host(store, "runner-a")

    row = await store.record_host_heartbeat(host="runner-a")

    assert row["runtime_versions"] == {"claude-code": "2.1.220"}
    assert row["runtime_versions_available"] == {"claude-code": ["2.1.220", "2.1.235"]}


async def test_a_selection_reaches_the_attempt_the_claim_stamps(store):
    """The feature, end to end in SQL: admin picks, the next attempt records it."""
    await _menu_host(store, "runner-a")
    assert await store.set_runtime_version(runtime="claude-code", version="2.1.235")
    await _create_job(store)  # runtime claude-code

    claim = await _menu_claim(store, "w1")

    assert claim is not None
    assert claim["runtime_version"] == "2.1.235"


async def test_a_version_no_host_offers_is_refused(store):
    """Refused rather than stored: the claim would fall back and say nothing."""
    await _menu_host(store, "runner-a", menu=["2.1.220"])

    assert not await store.set_runtime_version(runtime="claude-code", version="2.1.235")

    _, _, selected = await store.runner_pool()
    assert selected == {}


async def test_clearing_a_selection_returns_to_the_host_default(store):
    await _menu_host(store, "runner-a")
    await store.set_runtime_version(runtime="claude-code", version="2.1.235")

    assert await store.set_runtime_version(runtime="claude-code", version=None)
    await _create_job(store)
    claim = await _menu_claim(store, "w1")

    assert claim is not None
    assert claim["runtime_version"] == "2.1.220"


async def test_a_selection_the_image_lost_falls_back_instead_of_parking(store):
    """The drift the fallback exists for, reachable in exactly this order.

    A selection is only accepted while some host offers it — but the image can
    be rebuilt with a shorter menu afterwards, and then the stored choice names
    a version nothing installs. Refusing the claim there would park the queue
    on a configuration change nobody would connect to it; running the
    default is the smaller wrong, and the claim logs which it did.
    """
    await _menu_host(store, "runner-a")
    assert await store.set_runtime_version(runtime="claude-code", version="2.1.235")

    # The rebuild: same host, same name, an image that no longer holds 2.1.235.
    # The claiming replica says so itself, which is the evidence that counts —
    # its heartbeat may not have caught up mid-rollout.
    await _create_job(store)

    claim = await _menu_claim(store, "w1", menu=["2.1.220"])

    assert claim is not None, "an unservable selection must not park the queue"
    assert claim["runtime_version"] == "2.1.220"


async def test_physical_server_grouping_preserves_deployment_identity_and_labels(store):
    """Display metadata cannot merge credentials, brokers, capacity or placement."""
    for name in ("gpu-box", "gpu-box-kata"):
        await store.register_host(
            host=name,
            credential_hash=hash_secret(name),
            broker_url=f"http://{name}:8092",
            slots_total=3,
            labels={"zone": "test"},
        )
        await store.record_host_heartbeat(host=name, physical_host="gpu-box")
    await store.set_active_runner_host(host="gpu-box-kata")
    await store.set_host_status(host="gpu-box-kata", status=HOST_DRAINING)
    # Old replicas omitting the new field must not erase it.
    await store.record_host_heartbeat(host="gpu-box-kata")
    rows, active, _ = await store.runner_pool()
    assert active == "gpu-box-kata"
    assert len(rows) == 2
    for row in rows:
        assert row["labels"] == {"zone": "test", "physical_host": "gpu-box"}
        assert row["broker_url"] == f"http://{row['host']}:8092"
        assert row["slots_total"] == 3
        assert row["status"] == (HOST_DRAINING if row["host"] == active else HOST_ONLINE)
    assert (
        await store.authenticate_host(host="gpu-box", credential_hash=hash_secret("gpu-box-kata"))
        is None
    )
