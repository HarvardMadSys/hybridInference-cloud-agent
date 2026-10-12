"""Model-credential resolution, against a real database.

The unit tier drives this through fakes, so it pins what the *code* does with
rows it is handed. What only Postgres can settle is that the whole decision is
one fenced read rather than several checks in Python:

- **Five predicates have to hold at once** -- the attempt id, its generation,
  its `running` status, its unexpired lease, and the job's own
  `current_attempt_id` and state. Any one of them missing is an inference
  credential handed to a superseded turn, which is the failure this read
  exists to prevent.
- **The answer is the owner, the model and the MCP scope together.** Resolving
  a credential without the scope it was admitted under is how a relay forwards
  a tool call the job was never granted.

Setup goes through the store: a live claim is AttemptCommands' to create and
that capability has not moved yet. Every assertion goes through this
repository.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _expire_attempt,
    _settled,
)
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.credential_queries import PostgresCredentialQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-1"


@pytest_asyncio.fixture
async def db():
    """A clean database over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(config)

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
    """Everything this test needs, to set a live claim up."""
    return Storage(db)


@pytest_asyncio.fixture
async def credentials(db):
    """The capability under test, on the repository that owns it."""
    return PostgresCredentialQueries(db)


async def _live(store: Storage, **overrides):
    """A job with one live claim on it, and the claim."""
    job = await _create_job(store, user_id=_OWNER, **overrides)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    return job, claim


async def test_a_live_attempt_resolves_its_owner_model_and_scope(
    store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """All three travel together, which is the point of one fenced read."""
    job, claim = await _live(store)

    resolved = await credentials.resolve_model_credential(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert resolved is not None
    assert resolved["user_id"] == _OWNER
    assert resolved["job_id"] == job["id"]
    assert resolved["model"] == job["model"]
    assert isinstance(resolved["mcp_bindings"], list)
    assert resolved["thread_id"] == job["thread_id"]


async def test_a_superseded_generation_resolves_nothing(
    store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """A generation that is not the attempt's is a stale worker asking."""
    job, claim = await _live(store)

    assert (
        await credentials.resolve_model_credential(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"] + 1,
        )
        is None
    )


async def test_an_attempt_the_job_no_longer_points_at_resolves_nothing(
    db, store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """The job's own fence: a superseded attempt is not the current one.

    Only this predicate moves. The attempt still exists, still carries its
    generation, is still ``running`` and still holds a live lease, so a
    ``None`` here can only come from the job's own pointer -- which is what
    makes this a test of that predicate rather than of the read as a whole.
    """
    job, claim = await _live(store)

    assert (
        await credentials.resolve_model_credential(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is not None
    )

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET current_attempt_id = NULL WHERE id = $1", job["id"]
        )

    assert (
        await credentials.resolve_model_credential(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is None
    )


async def test_an_expired_lease_resolves_nothing(
    db, store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """The lease is the clock, and the query reads it."""
    job, claim = await _live(store)
    await _expire_attempt(db, store, claim["attempt_id"])

    assert (
        await credentials.resolve_model_credential(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is None
    )


async def test_a_settled_job_resolves_nothing(
    db, store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """Once the turn is over there is nothing left to authorize.

    A success carries its native ordinal, so the row is prepared before the
    transition -- the schema refuses a succeeded job without one -- and then
    the transition itself moves the state the read checks.
    """
    job, claim = await _live(store)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await _settled(db, store, job["id"])
    await store.attempt_commands.transition(**fence, from_states=("running",), to_state="succeeded")

    assert await credentials.resolve_model_credential(**fence) is None


async def test_a_stopped_turn_resolves_nothing(
    store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """Stop takes effect on the next tool call, not when the lease runs out.

    Cancelling a running turn only raises a flag: the job stays ``running``
    and the attempt keeps its lease so the worker can settle. The relay's
    fence reads that flag, so a stopped turn calls no more of the owner's
    tools — including one parked waiting for an approval.
    """
    job, claim = await _live(store)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await credentials.resolve_model_credential(**fence) is not None

    assert await store.attempt_commands.request_cancel(job_id=job["id"]) == "running"

    assert await credentials.resolve_model_credential(**fence) is None


async def test_a_removed_hosts_attempt_resolves_nothing(
    store: Storage, credentials: PostgresCredentialQueries
) -> None:
    """The MCP token names an attempt, never a host; the fence asks about both.

    Disabling the machine changes neither the attempt nor the job, so only
    the host predicate can be what refuses here.
    """
    job, claim = await _live(store)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await credentials.resolve_model_credential(**fence) is not None

    assert await store.host_pool.set_host_status(host="runner-a", status="disabled") == "ok"

    assert await credentials.resolve_model_credential(**fence) is None
