"""A dedicated test database, a job's usage reads over it, and residency fencing.

The fixtures here (``db``, ``store``, ``_seed_attempt``) are shared by the
storage tests that need a migrated database of their own.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    _reserve_shared_chat,
)
from tests.support.db import reset_agent_tables
from tests.support.follow_ups import append_follow_up
from tests.support.migrations import apply_migrations
from tests.support.usage import job_usage_inputs

from cloud_agent.ledger import summarize_job_usage
from cloud_agent.storage.assembly import Storage
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


def _fence(job: dict, claim: dict, sandbox: dict) -> dict:
    return {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }


async def _residency(store: Storage, job: dict, claim: dict, sandbox: dict) -> dict:
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
    grant_id: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> tuple[dict, int, int | None]:
    """Seed an attempt; ``grant_id`` also stores the ledger reference an earlier release kept."""
    job = await _create_job(store) if job is None else job
    started_at = started_at or datetime.now(UTC) - timedelta(seconds=10)
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


async def test_a_stale_residency_generation_is_refused_at_record(store):
    """The lease has one row now; a host echoing another generation is fenced out."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    residency = await _residency(store, job, claim, sandbox)
    stale = {**residency, "generation": residency["generation"] + 1}
    assert not await _record(store, job, claim, sandbox, stale)
    assert await _record(store, job, claim, sandbox, residency)


async def test_job_runtime_usage_includes_retries_but_excludes_other_turns(db, store):
    """Comparison data retains usage identities and output detection without transcript text."""
    job, first, _ = await _seed_attempt(db, store, grant_id=None)
    _, retry, _ = await _seed_attempt(db, store, job=job, attempt_no=2, grant_id=None)
    # A real second turn of the same thread: the schema ties a job's messages
    # and parent to its thread, so a job cannot simply be re-pointed.
    sibling = await append_follow_up(
        db, parent_job_id=job["id"], user_id=job["user_id"], prompt="a sibling turn"
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
    inputs = await job_usage_inputs(db, job_id=job["id"])
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
    base = datetime.now(UTC) - timedelta(minutes=10)
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
    thread = await store.execution_queries.thread_stats(job_id=job["id"], user_id=job["user_id"])
    assert reads is not None and thread is not None

    inputs = reads["inputs"]
    assert max(len(json.dumps(event["payload"])) for event in inputs["events"]) < 256
    expected = summarize_thread(job_ids=inputs["job_ids"], attempts=inputs["attempts"], events=full)
    assert summarize_thread(**inputs) == expected
    assert thread["stats"] == expected
    async with db.acquire() as conn:
        cached = await conn.fetch(
            "SELECT rollup::text AS rollup FROM agent_attempt_thread_stats "
            "WHERE attempt_id = ANY($1::bigint[]) ORDER BY attempt_id",
            [first, retry],
        )
    assert len(cached) == 2
    assert all(len(row["rollup"]) < 512 for row in cached)
    assert all(output not in row["rollup"] for row in cached)
    usage = summarize_job_usage(runtime_inputs=reads["inputs"], runtime="claude-code")
    assert usage == summarize_job_usage(
        runtime_inputs={**reads["inputs"], "events": full}, runtime="claude-code"
    )
    assert (usage.runtime.input_tokens, usage.runtime.output_tokens) == (142, 60)
