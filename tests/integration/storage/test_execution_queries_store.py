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
from tests.support.follow_ups import append_follow_up
from tests.support.migrations import apply_migrations
from tests.support.usage import job_usage_inputs

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-1"
_OTHER = "user-2"


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


async def test_a_stream_page_is_current_and_scoped_to_the_owner(
    store: Storage, queries: PostgresExecutionQueries, db
) -> None:
    """A wake-up's read: the next events and how the owned job stands, or nothing."""
    job, _ = await _running_job(store)

    page = await queries.job_stream_page(job_id=job["id"], user_id=_OWNER, after_id=0, limit=500)
    assert page is not None
    events, state = page
    assert [event["payload"] for event in events] == [
        {"text": "first", "seq": 1},
        {"text": "second", "seq": 2},
    ]
    assert state == JobStreamState("running", None)
    first, second = events
    page_of_one = await queries.job_stream_page(
        job_id=job["id"], user_id=_OWNER, after_id=0, limit=1
    )
    assert page_of_one == ([first], state)
    after_first = await queries.job_stream_page(
        job_id=job["id"], user_id=_OWNER, after_id=first["id"], limit=500
    )
    assert after_first == ([second], state)
    # Nothing new still says how the job stands; a stranger, or no job, gets nothing.
    caught_up = await queries.job_stream_page(
        job_id=job["id"], user_id=_OWNER, after_id=second["id"], limit=500
    )
    assert caught_up == ([], state)
    assert (
        await queries.job_stream_page(job_id=job["id"], user_id=_OTHER, after_id=0, limit=500)
        is None
    )
    assert (
        await queries.job_stream_page(job_id="ajob_missing", user_id=_OWNER, after_id=0, limit=500)
        is None
    )

    finished = await _create_job(store, user_id=_OWNER)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1, "
            "published_pr_url = $2 WHERE id = $1",
            finished["id"],
            "https://example.test/pr/42",
        )
    assert await queries.job_stream_page(
        job_id=finished["id"], user_id=_OWNER, after_id=0, limit=500
    ) == ([], JobStreamState("succeeded", "https://example.test/pr/42"))


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


async def test_trajectory_timings_are_one_query_for_one_hundred_turns_and_match_detail(db) -> None:
    """Three retries per turn stay one SQL statement and retain old semantics."""
    import asyncpg

    thread_id = "athread_trajectory_batch"
    job_ids = [f"ajob_trajectory_{number:03d}" for number in range(1, 101)]
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads (id, user_id, repo, title, execution_profile)
            VALUES ($1, $2, 'owner/name', 'Trajectory batch', 'trusted-v1')
            """,
            thread_id,
            _OWNER,
        )
        await conn.executemany(
            """
            INSERT INTO agent_jobs (
                id, thread_id, user_id, parent_job_id, turn_no,
                task_prompt, runtime, model, state
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'codex', 'gpt-5', 'running')
            """,
            [
                (
                    job_id,
                    thread_id,
                    _OWNER,
                    job_ids[index - 1] if index else None,
                    index + 1,
                    f"turn {index + 1}",
                )
                for index, job_id in enumerate(job_ids)
            ],
        )
        await conn.execute(
            """
            INSERT INTO agent_attempts (
                job_id, thread_id, attempt_no, lease_owner, lease_generation,
                lease_expires_at, status, started_at, finished_at,
                continuity_mode, context_status
            )
            SELECT job.id, job.thread_id, attempt_no, 'worker-test', attempt_no,
                   NOW() + INTERVAL '1 hour',
                   CASE WHEN attempt_no = 3 THEN 'running' ELSE 'finished' END,
                   TIMESTAMPTZ '2026-10-20 12:00:00+00'
                       + job.turn_no * INTERVAL '1 minute'
                       + attempt_no * INTERVAL '5 seconds',
                   CASE WHEN attempt_no = 3 THEN NULL
                        ELSE TIMESTAMPTZ '2026-10-20 12:00:00+00'
                             + job.turn_no * INTERVAL '1 minute'
                             + attempt_no * INTERVAL '5 seconds'
                             + attempt_no * INTERVAL '1 second' END,
                   CASE WHEN attempt_no = 1 THEN NULL
                        WHEN attempt_no = 2 THEN 'warm_native' ELSE 'hot' END,
                   CASE WHEN attempt_no = 1 THEN 'new'
                        WHEN attempt_no = 2 THEN 'restored' ELSE 'live' END
            FROM agent_jobs job
            CROSS JOIN generate_series(1, 3) AS attempt_no
            WHERE job.thread_id = $1
            """,
            thread_id,
        )
        await conn.execute(
            """
            INSERT INTO agent_attempt_resources (
                attempt_id, job_id, sampler_version, tool_time_ms, tool_calls_truncated
            )
            SELECT id, job_id, 'shared-member-v1', attempt_no * 100,
                   attempt_no = 2
            FROM agent_attempts
            WHERE thread_id = $1 AND attempt_no <= 2
            """,
            thread_id,
        )
        await conn.execute(
            """
            INSERT INTO agent_tool_call_resources (
                attempt_id, call_seq, tool_use_id, tool_name, timing_source
            )
            SELECT id, 1, 'tool-' || id, 'Read',
                   CASE WHEN attempt_no = 1 THEN 'exact' ELSE 'message' END
            FROM agent_attempts
            WHERE thread_id = $1 AND attempt_no <= 2
            """,
            thread_id,
        )

    logged = []

    async def attach_query_logger(conn) -> None:
        conn.add_query_logger(logged.append)

    reading_pool = await asyncpg.create_pool(
        **_database_config(),
        min_size=1,
        max_size=1,
        init=attach_query_logger,
    )
    try:
        batch_queries = PostgresExecutionQueries(reading_pool)
        requested = list(reversed(job_ids))
        result = await batch_queries.trajectory_timings(
            job_ids=requested, user_id=_OWNER, max_attempts=2_000
        )

        data_queries = [
            record.query
            for record in logged
            if not record.query.startswith("SELECT pg_advisory_unlock_all()")
        ]
        assert len(data_queries) == 1, data_queries
        assert "WITH requested AS MATERIALIZED" in data_queries[0]
        assert result["owned_count"] == 100
        assert result["attempt_count"] == 300
        assert len(result["attempts"]) == 300
        assert [row["job_id"] for row in result["attempts"][::3]] == requested
        assert [row["attempt_no"] for row in result["attempts"][:3]] == [1, 2, 3]
    finally:
        await reading_pool.close()

    representative = requested[0]
    old_attempts = await PostgresExecutionQueries(db).job_attempts_telemetry(job_id=representative)
    compact = [row for row in result["attempts"] if row["job_id"] == representative]
    assert [
        (
            row["attempt_id"],
            row["attempt_no"],
            row["continuity_mode"],
            row["context_status"],
            row["started_at"],
            row["finished_at"],
        )
        for row in compact
    ] == [
        (
            row["attempt_id"],
            row["attempt_no"],
            row["continuity_mode"],
            row["context_status"],
            row["started_at"],
            row["finished_at"],
        )
        for row in old_attempts
    ]
    for row in compact:
        detail = await PostgresExecutionQueries(db).job_turn_telemetry(
            job_id=representative, attempt_id=row["attempt_id"]
        )
        assert detail is not None
        calls = detail.get("tool_calls") or []
        assert row["tool_time_ms"] == detail.get("tool_time_ms")
        assert row["tool_calls_truncated"] is bool(detail.get("tool_calls_truncated"))
        assert row["tool_timing_exact"] is (
            bool(calls) and all(call["timing_source"] == "exact" for call in calls)
        )

    refused = await PostgresExecutionQueries(db).trajectory_timings(
        job_ids=[representative], user_id=_OTHER, max_attempts=2_000
    )
    assert refused == {"owned_count": 0, "attempt_count": 0, "attempts": []}
