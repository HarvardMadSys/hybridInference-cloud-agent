"""Real Postgres checks for reviewed projections and typed browser filters."""

from datetime import UTC, datetime

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import db as db, store as store

from cloud_agent.admin_database import DatabaseQuery, DatabaseQueryInvalid, DatabaseQueryTimeout

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_schema_and_rows_exclude_credentials_content_and_new_columns(db, store):
    job = await _create_job(
        store, task_prompt="PRIVATE-PROMPT", metadata={"credential": "PRIVATE-SECRET"}
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET title = 'PRIVATE-PROMPT-TITLE' WHERE id = $1",
            job["thread_id"],
        )
        await conn.execute(
            "UPDATE agent_jobs SET detail = 'PRIVATE-ERROR' WHERE id = $1", job["id"]
        )
        await conn.execute(
            "ALTER TABLE agent_jobs ADD COLUMN browser_future_secret TEXT DEFAULT 'PRIVATE-FUTURE'"
        )
    try:
        schema = await store.admin_database.list_tables()
        assert "agent_settings" not in [table.name for table in schema.tables]
        assert "agent_gitlab_connections" not in [table.name for table in schema.tables]
        jobs = next(table for table in schema.tables if table.name == "agent_jobs")
        assert jobs.primary_key == ["id"]
        assert {"task_prompt", "metadata", "detail", "browser_future_secret"}.isdisjoint(
            column.name for column in jobs.columns
        )
        threads = next(table for table in schema.tables if table.name == "agent_threads")
        assert "title" not in [column.name for column in threads.columns]
        page = await store.admin_database.query(DatabaseQuery(table="agent_jobs"))
        assert len(page.rows) == 1 and "PRIVATE" not in page.model_dump_json()
        for column in ("task_prompt", "metadata", "browser_future_secret"):
            with pytest.raises(DatabaseQueryInvalid):
                await store.admin_database.query(
                    DatabaseQuery.model_validate(
                        {
                            "table": "agent_jobs",
                            "filters": [{"column": column, "operator": "is_null"}],
                        }
                    )
                )
    finally:
        async with db.acquire() as conn:
            await conn.execute("ALTER TABLE agent_jobs DROP COLUMN browser_future_secret")


async def test_numeric_boolean_time_null_filters_and_stable_pagination(db, store):
    jobs = [await _create_job(store, user_id=f"owner-{number}") for number in range(3)]
    instant = datetime(2026, 10, 11, tzinfo=UTC)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET created_at = $1", instant)
    body = {
        "table": "agent_jobs",
        "order_by": "created_at",
        "limit": 1,
        "filters": [
            {"column": "turn_no", "operator": "gte", "value": "1"},
            {"column": "cancel_requested", "operator": "eq", "value": "false"},
            {"column": "created_at", "operator": "eq", "value": "2026-10-11T00:00:00Z"},
            {"column": "current_attempt_id", "operator": "is_null"},
        ],
    }
    seen = []
    for offset in range(4):
        result = await store.admin_database.query(
            DatabaseQuery.model_validate({**body, "offset": offset})
        )
        seen.extend(row[result.columns.index("id")] for row in result.rows)
        assert result.has_more == (offset < 2)
    assert seen == sorted(job["id"] for job in jobs)
    assert "2026-10-11T00:00:00+00:00" in result.model_dump_json() or result.rows == []


async def test_contains_treats_wildcards_and_sql_as_literal_data_and_large_numbers_are_exact(
    db, store
):
    job = await _create_job(store, user_id="literal_%owner")
    for value, count in [("_%", 1), ("%' OR true; DELETE FROM agent_jobs; --", 0)]:
        result = await store.admin_database.query(
            DatabaseQuery.model_validate(
                {
                    "table": "agent_jobs",
                    "filters": [{"column": "user_id", "operator": "contains", "value": value}],
                }
            )
        )
        assert len(result.rows) == count
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_runner_hosts (host, credential_hash) VALUES ('runner-1', 'PRIVATE-HASH')"
        )
        await conn.execute(
            """INSERT INTO agent_attempts
            (id, job_id, thread_id, attempt_no, lease_owner, lease_generation, lease_expires_at)
            VALUES ($1, $2, $3, 1, 'runner-1', 1, NOW() + INTERVAL '1 hour')""",
            2**53 + 1,
            job["id"],
            job["thread_id"],
        )
    result = await store.admin_database.query(
        DatabaseQuery.model_validate(
            {
                "table": "agent_attempts",
                "filters": [{"column": "id", "operator": "eq", "value": "9007199254740993"}],
            }
        )
    )
    assert result.rows[0][result.columns.index("id")] == "9007199254740993"
    assert (
        "PRIVATE-HASH"
        not in (
            await store.admin_database.query(DatabaseQuery(table="agent_runner_hosts"))
        ).model_dump_json()
    )


async def test_huge_text_is_shortened_with_visible_ellipsis_inside_response_budget(db, store):
    job = await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET model = $2 WHERE id = $1",
            job["id"],
            "prefix-" + "\u0001" * 20_000,
        )
    result = await store.admin_database.query(DatabaseQuery(table="agent_jobs", limit=200))
    value = result.rows[0][result.columns.index("model")]
    assert len(value) == result.text_limit and value.endswith("…")
    assert len(result.model_dump_json().encode()) < 2_000_000


async def test_reader_transaction_is_readonly_and_applies_local_timeout(db, store):
    async with store.admin_database._read() as conn:
        assert await conn.fetchval("SHOW transaction_read_only") == "on"
        assert await conn.fetchval("SHOW statement_timeout") == "5s"
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await conn.execute("DELETE FROM agent_jobs")


async def test_statement_timeout_is_reported_without_driver_diagnostics(db, store):
    with pytest.raises(DatabaseQueryTimeout):
        async with store.admin_database._read() as conn:
            await conn.execute("SET LOCAL statement_timeout = '10ms'")
            await conn.execute("SELECT pg_sleep(0.1)")
    assert (await store.admin_database.query(DatabaseQuery(table="agent_jobs"))).rows == []


async def test_locked_table_read_times_out_and_pool_recovers(db, store):
    async with db.acquire() as blocker, blocker.transaction():
        await blocker.execute("LOCK TABLE agent_jobs IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(DatabaseQueryTimeout):
            await store.admin_database.query(DatabaseQuery(table="agent_jobs"))
    assert (await store.admin_database.query(DatabaseQuery(table="agent_jobs"))).rows == []


async def test_sorting_uses_original_text_and_primary_key_after_projection_truncation(db, store):
    prefix = "x" * 10_000
    later = await _create_job(store)
    earlier = await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET model = $2 WHERE id = $1", later["id"], prefix + "z"
        )
        await conn.execute(
            "UPDATE agent_jobs SET model = $2 WHERE id = $1", earlier["id"], prefix + "a"
        )
    result = await store.admin_database.query(
        DatabaseQuery(table="agent_jobs", order_by="model", limit=1)
    )
    assert result.rows[0][result.columns.index("id")] == earlier["id"]
    result = await store.admin_database.query(
        DatabaseQuery(table="agent_jobs", order_by="model", limit=1, offset=1)
    )
    assert result.rows[0][result.columns.index("id")] == later["id"]
