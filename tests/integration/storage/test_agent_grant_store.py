"""The grant link's SQL predicates, against a real database.

``tests/unit/test_grant_lifecycle.py`` pins the *loops* — that they re-read the
store every pass and act only on what it returns. What it cannot pin is the
queries themselves, and those are where the fence actually lives: one wrong
predicate and ``list_renewable_grants`` starts returning superseded attempts,
with every unit test still green.

So these three properties are checked against Postgres:

- a superseded attempt's grant is **not** renewable and **is** revocable;
- an attempt whose lease lapsed behaves the same way, before any reaper has
  run — expiry is immediate, not "once the reaper notices";
- ``take_revocable_grants`` detaches as it hands over, so two callers cannot
  both be handed the same grant;
- the fence ``list_renewable_grants`` hands out is the one that will be
  presented back, when a grant the gateway keeps refusing makes the renewal
  pass end the attempt. A wrong column there is a give-up that silently does
  nothing, and the unit tests supply their own row.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.grants import PostgresGrantRepository
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
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
async def db():
    """Provide a clean Storage over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(db_config)
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)
        await conn.execute("UPDATE agent_runner_policy SET active_host = NULL")
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
            await conn.execute("UPDATE agent_runner_policy SET active_host = NULL")
        await pool.close()


@pytest_asyncio.fixture
async def store(db):
    """Everything else this test needs, to set a claim up."""
    return Storage(db)


@pytest_asyncio.fixture
async def grants(db):
    """The capability under test, on the repository that now owns it."""
    return PostgresGrantRepository(db)


async def _claimed(store: Storage, *, prompt: str = "fix it") -> dict:
    """Create a job and claim it through the enrolled shared-live contract."""
    credential_hash = "grant-runner-credential-hash"
    if await store.host_pool.get_host(host="grant-runner") is None:
        await store.host_pool.register_host(
            host="grant-runner",
            credential_hash=credential_hash,
            capabilities=[SHARED_SANDBOX_CAPABILITY],
            slots_total=8,
        )
        await store.host_pool.record_host_heartbeat(
            host="grant-runner",
            capabilities=[SHARED_SANDBOX_CAPABILITY],
        )
    await store.conversation_commands.create_job(
        user_id="user-owner",
        repo="owner/name",
        task_prompt=prompt,
        runtime="claude-code",
        model="glm-5.1",
    )
    claim = await store.attempt_commands.claim_job(
        worker_id="w1",
        lease_ttl_seconds=300.0,
        host="grant-runner",
        host_credential_hash=credential_hash,
        claim_attestation={
            "sandbox_image_digest": "sha256:grant-test-image",
            "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
            "runner_capabilities": [
                SHARED_SANDBOX_CAPABILITY,
                NATIVE_FORK_AT_ANCHOR_CAPABILITY,
                shared_deployment_capability("test-deployment"),
            ],
            "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
            "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
        },
    )
    assert claim is not None
    return claim


async def test_a_live_attempts_grant_is_renewable(
    store: Storage, grants: PostgresGrantRepository
) -> None:
    claim = await _claimed(store)

    assert await grants.record_attempt_grant(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        grant_id="agr_1",
    )

    renewable = await grants.list_renewable_grants()
    assert [row["grant_id"] for row in renewable] == ["agr_1"]
    assert await grants.take_revocable_grants() == []


async def test_a_renewable_row_carries_the_fence_that_can_end_it(
    store: Storage, grants: PostgresGrantRepository
) -> None:
    """The give-up path, end to end in SQL.

    ``_renew_live_grants`` abandons an attempt whose grant the gateway keeps
    refusing, and every write that ends an attempt is fenced. If the row's
    ``lease_generation`` were the wrong column — or absent — the release would
    match nothing and the give-up would be a log line: the attempt would run on
    with a capability about to expire, which is the failure the give-up exists
    to prevent.
    """
    claim = await _claimed(store)
    await grants.record_attempt_grant(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        grant_id="agr_1",
    )

    row = (await grants.list_renewable_grants())[0]
    assert await store.attempt_commands.release_claim(
        job_id=row["job_id"],
        attempt_id=row["attempt_id"],
        lease_generation=row["lease_generation"],
    )

    # And the abandoned attempt's grant is now the revocation pass's problem,
    # exactly once — nothing had to revoke it inline.
    assert await grants.list_renewable_grants() == []
    assert [taken["grant_id"] for taken in await grants.take_revocable_grants()] == ["agr_1"]
    assert await grants.take_revocable_grants() == []


async def test_a_superseded_attempts_grant_flips_sides(
    db, store: Storage, grants: PostgresGrantRepository
) -> None:
    """The fence, in SQL. One reaper pass moves the grant from one set to the
    other, and nothing else has to be told."""
    claim = await _claimed(store)
    await grants.record_attempt_grant(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        grant_id="agr_1",
    )

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            claim["attempt_id"],
        )

    # Immediately, before any reaper pass: an expired lease is dead now.
    assert await grants.list_renewable_grants() == []

    await store.attempt_commands.reap_expired()
    assert await grants.list_renewable_grants() == []
    assert [row["grant_id"] for row in await grants.take_revocable_grants()] == ["agr_1"]


async def test_a_grant_is_handed_over_exactly_once(
    db, store: Storage, grants: PostgresGrantRepository
) -> None:
    """``take_`` detaches as it returns, so a second pass — or a second control
    plane — is not handed the same grant to revoke again."""
    claim = await _claimed(store)
    await grants.record_attempt_grant(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        grant_id="agr_1",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET status = 'superseded', finished_at = NOW() WHERE id = $1",
            claim["attempt_id"],
        )

    assert len(await grants.take_revocable_grants()) == 1
    assert await grants.take_revocable_grants() == []


async def test_recording_against_a_stale_fence_is_refused(
    store: Storage, grants: PostgresGrantRepository
) -> None:
    """A grant minted for an attempt that was superseded meanwhile must not be
    attached to it: it would then be renewed for as long as the row lived."""
    claim = await _claimed(store)

    assert not await grants.record_attempt_grant(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        grant_id="agr_1",
    )
    assert await grants.list_renewable_grants() == []


async def test_an_admin_action_is_recorded(db, store: Storage) -> None:
    """The audit trail, written through the store, says who and from where."""
    row_id = await store.audit.record_admin_action(
        action="agent.runner_host.drain",
        actor_id="admin-1",
        admin_ip="203.0.113.9",
        details={"host": "runner-a"},
    )

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action, actor_id, admin_ip, details, success "
            "FROM agent_admin_audit_log WHERE id = $1",
            row_id,
        )
        await conn.execute("DELETE FROM agent_admin_audit_log WHERE id = $1", row_id)

    assert row["action"] == "agent.runner_host.drain"
    assert row["actor_id"] == "admin-1"
    assert row["admin_ip"] == "203.0.113.9"
    assert row["success"] is True
