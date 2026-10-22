"""Execution reads, against a real database.

The unit tier drives these through fakes, so it pins what the *code* does with
rows it is handed. What only Postgres can settle:

- **The scoping is in the query.** `list_thread_events_after` and
  `thread_stats` answer ``None`` for a job that is not the caller's --
  the same answer they give for a job that does not exist, which is what stops
  the difference between the two from being observable.
- **The aggregate reads are scoped too.** `thread_stats` joins the thread
  through the same owner check as the event read, so a stranger's summary is
  as unavailable as their transcript. It refreshes only stale attempt rollups.
- **Payloads survive the round trip.** `list_thread_events_after` keeps them,
  because reconstructing what an earlier turn did needs the tool inputs, and
  JSONB comes back as text from a bare fetch -- the read decodes it.

Setup goes through the store: a job and its claim are ConversationCommands' and
AttemptCommands' to create, and neither has moved. The assertions go through
this repository.
"""

from __future__ import annotations

import gc
import os
import time
import tracemalloc
from typing import Any

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _claim, _create_job
from tests.support.db import reset_agent_tables
from tests.support.follow_ups import append_follow_up
from tests.support.migrations import apply_migrations
from tests.support.usage import job_usage_inputs

from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-1"
_OTHER = "user-2"


class _MeasuredConnection:
    """Count repository statements and rows returned by the stale-event read."""

    def __init__(self, connection: Any, measurements: dict[str, int]) -> None:
        self._connection = connection
        self._measurements = measurements

    async def fetch(self, query: str, *args: Any):
        self._measurements["queries"] += 1
        rows = await self._connection.fetch(query, *args)
        if "thread_stats_stale_events" in query:
            self._measurements["event_rows"] += len(rows)
        return rows

    async def execute(self, query: str, *args: Any):
        self._measurements["queries"] += 1
        return await self._connection.execute(query, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _MeasuredAcquire:
    def __init__(self, pool: Any, measurements: dict[str, int]) -> None:
        self._pool = pool
        self._measurements = measurements
        self._context: Any = None

    async def __aenter__(self) -> _MeasuredConnection:
        self._context = self._pool.acquire()
        connection = await self._context.__aenter__()
        return _MeasuredConnection(connection, self._measurements)

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._context.__aexit__(*exc)


class _MeasuredPool:
    def __init__(self, pool: Any, measurements: dict[str, int]) -> None:
        self._pool = pool
        self._measurements = measurements

    def acquire(self) -> _MeasuredAcquire:
        return _MeasuredAcquire(self._pool, self._measurements)


def _database_config() -> dict[str, str | int]:
    """The isolated test database selected for this pytest worker."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def db():
    """A clean database over the shared test database."""
    import asyncpg

    config = _database_config()
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


async def test_the_thread_read_pages_identically_when_other_threads_interleave(
    db, store: Storage, queries: PostgresExecutionQueries
) -> None:
    """Every page is the same rows in the same order as one unpaged read.

    A stranger's thread is created between this thread's turns, so its events
    interleave in the id range, and the thread's own events span turns
    (jobs) -- exactly the shape the page-ids-first statement must not distort
    (issue #810).
    """
    parent, parent_claim = await _running_job(store)
    stranger_job, stranger_claim = await _running_job(store)
    assert stranger_claim is not None and stranger_job["id"] == stranger_claim["id"]
    turns = [parent]
    # The parent's turn must go terminal before a follow-up can claim;
    # "failed" is terminal without the native-context settlement "succeeded"
    # requires, which this read never exercises.
    await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
    )
    for number in range(2, 5):
        follow_up = await append_follow_up(
            db, parent_job_id=turns[-1]["id"], user_id=_OWNER, prompt=f"turn {number}"
        )
        assert follow_up is not None
        claim = await _claim(
            store,
            worker_id=f"w{number}",
            lease_ttl_seconds=60,
            host="runner-a",
        )
        assert claim is not None and claim["id"] == follow_up["id"]
        for seq in range(1, 4):
            await store.attempt_commands.append_event(
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                event_type="message",
                payload={"text": f"t{number}-{seq}", "seq": seq},
            )
        await store.attempt_commands.transition(
            job_id=follow_up["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="failed",
        )
        turns.append(follow_up)
    # Extra events on the stranger's job, interleaved after this thread's.
    for seq in range(3, 8):
        await store.attempt_commands.append_event(
            attempt_id=stranger_claim["attempt_id"],
            lease_generation=stranger_claim["lease_generation"],
            event_type="message",
            payload={"text": f"stranger-{seq}", "seq": seq},
        )

    pages: list[dict] = []
    after = 0
    while True:
        page = await queries.list_thread_events_after(
            job_id=parent["id"], user_id=_OWNER, after_id=after, limit=4
        )
        if page is None or not page:
            break
        pages.extend(page)
        after = page[-1]["id"]
    everything = await queries.list_thread_events_after(job_id=parent["id"], user_id=_OWNER)
    assert everything is not None
    # Paged reads reconstruct exactly the unpaged read: no missing, no
    # duplicate, no stranger row, same order.
    assert [event["id"] for event in pages] == [event["id"] for event in everything]
    assert [event["payload"]["text"] for event in everything] == [
        "first",
        "second",
        "t2-1",
        "t2-2",
        "t2-3",
        "t3-1",
        "t3-2",
        "t3-3",
        "t4-1",
        "t4-2",
        "t4-3",
    ]
    assert {event["job_id"] for event in everything} == {turn["id"] for turn in turns}


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

    assert await queries.thread_stats(job_id=job["id"], user_id=_OWNER) is not None
    assert await queries.thread_stats(job_id=job["id"], user_id=_OTHER) is None


async def test_the_stats_read_persists_and_reuses_attempt_rollups(
    db, store: Storage, queries: PostgresExecutionQueries
) -> None:
    """One read reduces stale events and leaves a reusable attempt summary."""
    job, claim = await _running_job(store)

    result = await queries.thread_stats(job_id=job["id"], user_id=_OWNER)

    assert result is not None
    assert result["thread_id"] == job["thread_id"]
    assert result["stats"].turns == 1
    async with db.acquire() as conn:
        cached = await conn.fetchrow(
            "SELECT event_seq, reducer_version, rollup "
            "FROM agent_attempt_thread_stats WHERE attempt_id = $1",
            claim["attempt_id"],
        )
    assert cached is not None
    assert cached["event_seq"] == 2
    assert cached["reducer_version"] == 1

    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="tool_use",
        payload={"id": "after-cache"},
    )
    refreshed = await queries.thread_stats(job_id=job["id"], user_id=_OWNER)
    assert refreshed is not None and refreshed["stats"].tool_calls == 1
    async with db.acquire() as conn:
        sequence = await conn.fetchval(
            "SELECT event_seq FROM agent_attempt_thread_stats WHERE attempt_id = $1",
            claim["attempt_id"],
        )
        await conn.execute(
            "UPDATE agent_attempt_thread_stats "
            "SET reducer_version = 999, rollup = '{\"invalid\":true}'::jsonb "
            "WHERE attempt_id = $1",
            claim["attempt_id"],
        )
    assert sequence == 3

    rebuilt = await queries.thread_stats(job_id=job["id"], user_id=_OWNER)
    assert rebuilt == refreshed
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT reducer_version FROM agent_attempt_thread_stats WHERE attempt_id = $1",
                claim["attempt_id"],
            )
            == 1
        )


async def test_unchanged_hundred_thousand_event_stats_read_is_bounded(db, store: Storage) -> None:
    """A warm summary transfers no event rows and allocates independently of history."""
    job = await _create_job(store, user_id=_OWNER)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_job_events
                (job_id, attempt_id, seq, event_type, payload, created_at)
            SELECT $1,
                   $2,
                   n,
                   CASE n % 1000
                       WHEN 1 THEN 'tool_use'
                       WHEN 2 THEN 'tool_result'
                       WHEN 3 THEN 'message'
                       ELSE 'raw'
                   END,
                   CASE n % 1000
                       WHEN 1 THEN jsonb_build_object('id', 'tool-' || n::text)
                       WHEN 2 THEN jsonb_build_object(
                           'tool_use_id', 'tool-' || (n - 1)::text,
                           'is_error', n % 2000 = 2
                       )
                       WHEN 3 THEN jsonb_build_object(
                           'has_text', true,
                           'usage_id', 'response-' || n::text,
                           'usage', jsonb_build_object(
                               'input_tokens', 10,
                               'output_tokens', 5,
                               'cache_read_input_tokens', 2
                           )
                       )
                       ELSE '{}'::jsonb
                   END,
                   TIMESTAMPTZ '2026-01-01 00:00:00+00'
                       + n * INTERVAL '1 millisecond'
            FROM generate_series(1, 100000) AS generated(n)
            """,
            job["id"],
            claim["attempt_id"],
        )

    cold_counts = {"queries": 0, "event_rows": 0}
    measured = PostgresExecutionQueries(_MeasuredPool(db, cold_counts))
    started = time.perf_counter()
    cold = await measured.thread_stats(job_id=job["id"], user_id=_OWNER)
    cold_seconds = time.perf_counter() - started
    assert cold is not None
    assert cold_counts == {"queries": 3, "event_rows": 100_000}
    assert cold["stats"].tool_calls == 100
    assert cold["stats"].failed_tool_calls == 50
    assert cold["stats"].input_tokens == 1_000
    assert cold["stats"].output_tokens == 500

    warm_counts = {"queries": 0, "event_rows": 0}
    measured = PostgresExecutionQueries(_MeasuredPool(db, warm_counts))
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        warm = await measured.thread_stats(job_id=job["id"], user_id=_OWNER)
        warm_seconds = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert warm == cold
    assert warm_counts == {"queries": 1, "event_rows": 0}
    assert peak_bytes < 4 * 1024 * 1024
    assert warm_seconds < 2.0
    assert warm_seconds < cold_seconds


async def test_the_usage_inputs_and_telemetry_answer_for_their_job(
    db, store: Storage, queries: PostgresExecutionQueries
) -> None:
    """Three aggregates read the same turn from different tables."""
    job, claim = await _running_job(store)

    usage = await job_usage_inputs(db, job_id=job["id"])
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
