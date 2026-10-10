"""Measure the Admin Usage selection on a realistic local dataset.

This is a measurement tool, not a test. It seeds a dedicated ``_test_``
database, reports the query plan and latency of the two statements the admin
usage route runs, and then reports the time and size of the whole request.

Usage:
    TEST_DB_NAME=cloud_agent_usage_measure_test_db python scripts/measure_admin_usage.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

import asyncpg
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.admin_usage import (
    MAX_USAGE_LEDGER_ROWS,
    MAX_USAGE_RUNS,
    MAX_USAGE_SNAPSHOT_BYTES,
    AdminUsageAggregate,
    AdminUsageBreakdown,
    aggregate_prepared,
    breakdown,
    day_buckets,
    days_breakdown,
    prepare_jobs,
    resolve_usage_window,
)
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.storage.admin_usage_queries import (
    _BUDGET_SQL,
    _COHORT_SQL,
    _PROJECTION_SQL,
    AdminUsageQueries,
)

OWNERS = [f"owner-{index:02d}" for index in range(5)]
MODELS = ["model-a", "model-b", "model-c", ""]
JOBS = 3_000
EVENTS = 200_000
DAYS = 7
WINDOW_START = datetime(2026, 9, 9, tzinfo=UTC)


def config() -> dict[str, object]:
    """Read the guarded test database settings, as the dbtest fixtures do."""
    name = os.getenv("TEST_DB_NAME", "cloud_agent_usage_measure_test_db")
    if "_test_" not in name:
        raise SystemExit("refusing to seed a database whose name does not contain _test_")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


def _reduce(
    cohort: list[dict], rows: list[dict], since: datetime, until: datetime
) -> tuple[AdminUsageAggregate, list, AdminUsageBreakdown, AdminUsageBreakdown]:
    """Run the whole in-Python half of the request, exactly as the route does."""
    ledger_by_job: dict[str, list[dict]] = {row["id"]: [] for row in cohort}
    for row in rows:
        ledger_by_job[row["job_id"]].append(row)
    jobs = [{**row, "ledger": ledger_by_job[row["id"]]} for row in cohort]
    prepared = prepare_jobs(jobs, terminal_states=TERMINAL_STATES)
    return (
        aggregate_prepared(prepared),
        days_breakdown(
            jobs,
            buckets=day_buckets(since=since, until=until),
            terminal_states=TERMINAL_STATES,
            prepared=prepared,
        ),
        breakdown(jobs, dimension="user_id", terminal_states=TERMINAL_STATES, prepared=prepared),
        breakdown(jobs, dimension="model", terminal_states=TERMINAL_STATES, prepared=prepared),
    )


def report(*, calls: int, amount: str) -> str:
    """A stored snapshot of the size a real Gateway totals report occupies."""
    return json.dumps(
        {
            "schema_version": 1,
            "source": "gateway_api_logs",
            "grant_id": "grant-seed",
            "external_job_id": "thread:seed",
            "attribution": "grant_time_window",
            "window": {
                "since": "2026-09-09T10:00:00+00:00",
                "until": "2026-09-09T10:01:00+00:00",
                "basis": "request_started_at",
            },
            "as_of": "2026-09-09T10:01:00+00:00",
            "finalized": False,
            "totals": {
                "calls": calls,
                "tokens_in": 12_345,
                "tokens_out": 678,
                "cache_read": 1_000,
                "cache_write": 0,
                "reasoning": None,
                "spent_usd": amount,
            },
            "unknown_rows": {
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": calls,
                "spent_usd": 0,
            },
            "estimated_calls": 0,
        }
    )


async def seed(pool: asyncpg.Pool) -> None:
    """Seed owners, models, days, retries, historical gaps and event volume."""
    async with pool.acquire() as conn:
        # The same reset the dbtest fixtures use, so the seed is repeatable.
        await reset_agent_tables(conn)
        threads = [
            (
                f"athr_{index:05d}",
                OWNERS[index % len(OWNERS)],
                "example/repo",
                f"seeded task {index}",
                "untrusted-kata-v1",
                WINDOW_START,
            )
            for index in range(JOBS)
        ]
        await conn.executemany(
            "INSERT INTO agent_threads"
            " (id, user_id, repo, title, execution_profile, created_at, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $6)",
            threads,
        )
        jobs = [
            (
                f"ajob_{index:05d}",
                f"athr_{index:05d}",
                1,
                OWNERS[index % len(OWNERS)],
                "fix the flaky test",
                "claude-code",
                MODELS[index % len(MODELS)],
                "succeeded" if index % 17 else "failed",
                WINDOW_START + timedelta(hours=index % (DAYS * 24 - 1)),
            )
            for index in range(JOBS)
        ]
        await conn.executemany(
            """
            INSERT INTO agent_jobs (
                id, thread_id, turn_no, user_id, task_prompt, runtime, model,
                state, native_turn_no, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                      CASE WHEN $8 = 'succeeded' THEN 1 END, $9, $9)
            """,
            jobs,
        )
        attempts = []
        for index in range(JOBS):
            # One attempt per job, plus a retry every tenth job.
            attempts.append((f"ajob_{index:05d}", f"athr_{index:05d}", 1))
            if index % 10 == 0:
                attempts.append((f"ajob_{index:05d}", f"athr_{index:05d}", 2))
        await conn.executemany(
            """
            INSERT INTO agent_attempts (
                job_id, thread_id, attempt_no, lease_owner, lease_generation,
                lease_expires_at, started_at, finished_at, status
            ) VALUES ($1, $2, $3, 'seed', 1, NOW() + INTERVAL '1 hour', $4, $5, 'finished')
            """,
            [
                (job_id, thread_id, attempt_no, WINDOW_START, WINDOW_START + timedelta(minutes=1))
                for job_id, thread_id, attempt_no in attempts
            ],
        )
        await conn.execute(
            """
            INSERT INTO agent_attempt_ledger (
                attempt_id, grant_id, residency_generation, binding_id, window_since,
                snapshot, queried_at, terminal_refresh_count
            )
            SELECT a.id, 'grant-seed', 1, 'gateway-binding-seed', $1, NULL, NULL, 2
            FROM agent_attempts a
            """,
            WINDOW_START,
        )
        # 92% of executions have a readable snapshot. Another 5% keep a
        # reference that has not been read yet, and 3% keep a reference with a
        # snapshot the reader must reject.
        await conn.execute(
            """
            UPDATE agent_attempt_ledger SET snapshot = $1::jsonb, queried_at = $2
            WHERE id IN (
                SELECT id FROM agent_attempt_ledger ORDER BY id LIMIT 3005
            )
            """,
            report(calls=3, amount="0.01234567"),
            WINDOW_START + timedelta(minutes=2),
        )
        await conn.execute(
            """
            UPDATE agent_attempt_ledger SET snapshot = '{"schema_version": 1}'::jsonb
            WHERE id IN (SELECT id FROM agent_attempt_ledger ORDER BY id DESC LIMIT 100)
            """
        )
        await conn.execute(
            """
            UPDATE agent_attempt_ledger SET next_refresh_at = $1
            WHERE id IN (
                SELECT id FROM agent_attempt_ledger ORDER BY id OFFSET 3005 LIMIT 162
            )
            """,
            WINDOW_START + timedelta(hours=1),
        )
        attempts_with_ledger = await conn.fetchval("SELECT COUNT(*) FROM agent_attempt_ledger")
        jobs_with_ledger = await conn.fetchval(
            "SELECT COUNT(DISTINCT job_id) FROM agent_attempts a"
            " JOIN agent_attempt_ledger l ON l.attempt_id = a.id"
        )
        # Unrelated events on the seeded jobs. The event table is deliberately
        # large: aggregation must not scan it, and this is the volume that would
        # make a per-job count visible in the plan.
        per_job = EVENTS // JOBS + 1
        job_ids = [f"ajob_{index:05d}" for index in range(JOBS) for _ in range(per_job)]
        sequences = [seq for _ in range(JOBS) for seq in range(1, per_job + 1)]
        payloads = [json.dumps({"text": "unrelated event payload " * 20})] * len(job_ids)
        await conn.execute(
            """
            INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)
            SELECT j.job_id, a.id, j.seq, 'message', j.payload::jsonb
            FROM unnest($1::text[], $2::int[], $3::text[]) AS j(job_id, seq, payload)
            JOIN agent_attempts a ON a.job_id = j.job_id AND a.attempt_no = 1
            """,
            job_ids,
            sequences,
            payloads,
        )
        snapshots = await conn.fetchval(
            "SELECT COALESCE(SUM(OCTET_LENGTH(snapshot::text)), 0) FROM agent_attempt_ledger"
        )
    print(
        f"seeded jobs={JOBS} ledger_rows={attempts_with_ledger} "
        f"jobs_with_a_snapshot={jobs_with_ledger} events={EVENTS} "
        f"snapshot_bytes={snapshots}"
    )


async def explain(conn: asyncpg.Connection, sql: str, *args: object) -> None:
    """Print the planner's plan for one statement."""
    plan = await conn.fetch("EXPLAIN (ANALYZE, BUFFERS) " + sql, *args)
    print("\n".join(row[0] for row in plan))


async def main() -> None:
    """Seed, plan, and time one admin usage selection."""
    settings = config()
    apply_migrations(settings)
    pool = await asyncpg.create_pool(**settings, min_size=1, max_size=5)
    try:
        await seed(pool)
        since, until = resolve_usage_window(
            since=WINDOW_START, until=WINDOW_START + timedelta(days=DAYS)
        )
        queries = AdminUsageQueries(pool)

        started = time.perf_counter()
        cohort, rows = await queries.selection(
            since=since,
            until=until,
            user_id=None,
            model=None,
            runtime=None,
            repo=None,
            max_runs=MAX_USAGE_RUNS,
            max_rows=MAX_USAGE_LEDGER_ROWS,
            max_snapshot_bytes=MAX_USAGE_SNAPSHOT_BYTES,
        )
        read_ms = (time.perf_counter() - started) * 1000
        print(
            f"\nselection: {len(cohort)} jobs and {len(rows)} ledger rows "
            f"from one read view in {read_ms:.1f} ms"
        )

        # Three runs of the reduction, because one sample of a 100 ms step on a
        # shared machine is not a measurement.
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            _reduce(cohort, rows, since, until)
            timings.append((time.perf_counter() - started) * 1000)
        python_ms = min(timings)
        summary, days, by_user, by_model = _reduce(cohort, rows, since, until)
        print(
            f"python reduction: {python_ms:.1f} ms (best of {len(timings)}) for "
            f"{summary.run_count} runs, "
            f"{summary.attempt_count} executions, {by_user.group_count} users, "
            f"{by_model.group_count} models, {len(days)} days"
        )
        payload = {
            "summary": summary.model_dump(mode="json"),
            "days": [day.model_dump(mode="json") for day in days],
            "by_user": by_user.model_dump(mode="json"),
            "by_model": by_model.model_dump(mode="json"),
        }
        print(f"response: {len(json.dumps(payload).encode()) / 1024:.0f} KiB")

        async with pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '5s'")
            print("\n--- cohort plan ---")
            await explain(
                conn,
                _COHORT_SQL,
                since,
                until,
                None,
                None,
                None,
                None,
                MAX_USAGE_RUNS + 1,
            )
            print("\n--- budget plan ---")
            await explain(conn, _BUDGET_SQL, [row["id"] for row in cohort], since, until)
            print("\n--- projection plan ---")
            await explain(conn, _PROJECTION_SQL, [row["id"] for row in cohort], since, until)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
