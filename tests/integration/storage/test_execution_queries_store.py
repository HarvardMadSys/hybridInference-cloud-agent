"""Execution reads, against a real database.

The unit tier drives these through fakes, so it pins what the *code* does with
rows it is handed. What only Postgres can settle:

- **The scoping is in the query.** `list_thread_events_after` and
  `thread_stats_inputs` answer ``None`` for a job that is not the caller's --
  the same answer they give for a job that does not exist, which is what stops
  the difference between the two from being observable.
- **The aggregate reads are scoped too.** `thread_stats_inputs` joins the
  thread through the same owner check as the event read, so a stranger's
  summary is as unavailable as their transcript. It returns the thread's jobs,
  attempts and events together, because the caller summarizes across all of
  them rather than one turn at a time.
- **Payloads survive the round trip.** `list_thread_events_after` keeps them,
  because reconstructing what an earlier turn did needs the tool inputs, and
  JSONB comes back as text from a bare fetch -- the read decodes it.

Setup goes through the store: a job and its claim are ConversationCommands' and
AttemptCommands' to create, and neither has moved. The assertions go through
this repository.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _claim, _create_job
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-1"
_OTHER = "user-2"


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
    """Everything this test needs, to create a job, claim it and append."""
    return Storage(db)


@pytest_asyncio.fixture
async def queries(db):
    """The capability under test, on the repository that owns it."""
    return PostgresExecutionQueries(db)


async def _running_job(store: Storage, **overrides):
    """A job with one live claim and two appended events."""
    job = await _create_job(store, user_id=_OWNER, **overrides)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for seq, text in enumerate(["first", "second"], start=1):
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"text": text, "seq": seq},
        )
    return job, claim


# ── Thread and job event reads ────────────────────────────────────────


async def test_the_thread_read_answers_nothing_for_a_stranger(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """Absence and refusal are the same answer, which callers rely on."""
    job, _ = await _running_job(store)

    assert await queries.list_thread_events_after(job_id=job["id"], user_id=_OWNER) is not None
    assert await queries.list_thread_events_after(job_id=job["id"], user_id=_OTHER) is None
    assert await queries.list_thread_events_after(job_id="ajob_missing", user_id=_OWNER) is None


async def test_the_thread_read_returns_decoded_payloads_in_order(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """A payload is JSONB, and reconstructing a turn needs it decoded."""
    job, _ = await _running_job(store)

    events = await queries.list_thread_events_after(job_id=job["id"], user_id=_OWNER)

    assert events is not None
    assert [event["payload"]["text"] for event in events] == ["first", "second"]
    assert [event["seq"] for event in events] == [1, 2]


async def test_the_thread_read_pages_by_event_id(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """``after_id`` is the caller's cursor into a long thread."""
    job, _ = await _running_job(store)
    everything = await queries.list_thread_events_after(job_id=job["id"], user_id=_OWNER)
    assert everything is not None and len(everything) == 2

    after_first = await queries.list_thread_events_after(
        job_id=job["id"], user_id=_OWNER, after_id=everything[0]["id"]
    )

    assert after_first is not None
    assert [event["payload"]["text"] for event in after_first] == ["second"]


async def test_the_per_job_read_returns_the_same_turn(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """The per-job read is scoped to the turn being watched, not the thread."""
    job, _ = await _running_job(store)

    events = await queries.list_events_after(job_id=job["id"])

    assert [event["payload"]["text"] for event in events] == ["first", "second"]


# ── Aggregates ────────────────────────────────────────────────────────


async def test_the_stats_inputs_answer_nothing_for_a_stranger(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """The summary of a thread is as private as the thread."""
    job, _ = await _running_job(store)

    assert await queries.thread_stats_inputs(job_id=job["id"], user_id=_OWNER) is not None
    assert await queries.thread_stats_inputs(job_id=job["id"], user_id=_OTHER) is None


async def test_the_stats_inputs_carry_the_threads_jobs_attempts_and_events(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """One read gathers everything a summary needs, across the whole thread."""
    job, _ = await _running_job(store)

    inputs = await queries.thread_stats_inputs(job_id=job["id"], user_id=_OWNER)

    assert inputs is not None
    assert inputs["thread_id"] == job["thread_id"]
    assert inputs["job_ids"] == [job["id"]]
    assert [row["job_id"] for row in inputs["attempts"]] == [job["id"]]
    assert len(inputs["events"]) == 2


async def test_the_usage_inputs_and_telemetry_answer_for_their_job(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """Three aggregates read the same turn from different tables."""
    job, claim = await _running_job(store)

    usage = await queries.job_usage_inputs(job_id=job["id"])
    attempts = await queries.job_attempts_telemetry(job_id=job["id"])
    turn = await queries.job_turn_telemetry(job_id=job["id"], attempt_id=claim["attempt_id"])

    assert usage["job_ids"] == [job["id"]]
    assert [row["attempt_id"] for row in attempts] == [claim["attempt_id"]]
    assert turn is not None and turn["processes"] == []


async def test_turn_telemetry_answers_nothing_for_an_attempt_the_job_never_had(
    store: Storage, queries: PostgresExecutionQueries
) -> None:
    """An attempt id that is not this job's is not this job's telemetry."""
    job, claim = await _running_job(store)

    assert (
        await queries.job_turn_telemetry(job_id=job["id"], attempt_id=claim["attempt_id"] + 10_000)
        is None
    )
