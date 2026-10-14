"""The admin listings read every owner's turns in creation order through an index.

Without one, a listing with no owner filter scanned and sorted the whole job
history on every page, under the admin reads' 5 s statement timeout. The plan
checks run the statements the admin queries actually send, captured on the
wire, so a later edit that stops the index from serving them fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.admin_observability import RunFilters
from cloud_agent.storage.admin_queries import AdminRunQueries
from cloud_agent.storage.admin_usage_queries import AdminUsageQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0065_environments"
_THIS = "0066_admin_job_listing_index"
_INDEX = "idx_agent_jobs_created"
#: Enough history that reading all of it is the plan to avoid, one turn a minute.
_TURNS = 5000


async def _index(pool) -> asyncpg.Record | None:
    """Whether the index exists, whether it is valid, and what it covers."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition "
            "FROM pg_index i WHERE i.indexrelid = to_regclass($1)",
            _INDEX,
        )


async def test_an_interrupted_concurrent_build_is_replaced_by_a_valid_index(store, pool):
    """A concurrent build that failed leaves an invalid index; the upgrade rebuilds it.

    ``IF NOT EXISTS`` would have kept the broken one, which the planner never
    uses, so the upgrade would have reported success over an index that does
    nothing.
    """
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        assert await _index(pool) is None
        # Two turns of one owner fail a unique build part-way, which is how an
        # interrupted ``CONCURRENTLY`` build leaves its index behind.
        await _create_job(store)
        await _create_job(store)
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {_INDEX} ON agent_jobs (user_id)"
                )
        leftover = await _index(pool)
        assert leftover is not None and leftover["indisvalid"] is False

        apply_migrations(config, _THIS)

        built = await _index(pool)
        assert built is not None and built["indisvalid"] is True
        assert built["definition"].endswith("USING btree (created_at DESC, id DESC)")

        downgrade_to(config, _BEFORE)
        assert await _index(pool) is None
    finally:
        apply_migrations(config)


async def _seed_history(pool) -> None:
    """``_TURNS`` single-turn conversations across many owners, one a minute."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads
                (id, user_id, repo, title, title_source, setup_script, execution_profile)
            SELECT 'athr_hist_' || g, 'user-' || (g % 50), 'example-org/example-repo',
                   'history', 'fallback', NULL, 'trusted-v1'
            FROM generate_series(1, $1) g
            """,
            _TURNS,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs
                (id, thread_id, user_id, turn_no, task_prompt, runtime, model, state,
                 native_turn_no, created_at, updated_at)
            SELECT 'ajob_hist_' || g, 'athr_hist_' || g, 'user-' || (g % 50), 1, 'p',
                   'claude-code', 'glm-5.1', 'succeeded', 1,
                   NOW() - make_interval(mins => g), NOW()
            FROM generate_series(1, $1) g
            """,
            _TURNS,
        )
        await conn.execute("ANALYZE agent_threads")
        await conn.execute("ANALYZE agent_jobs")


async def _plan(pool, statement: Any) -> str:
    """The plan PostgreSQL chooses for one captured statement and its arguments."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"EXPLAIN {statement.query}", *statement.args)
    return "\n".join(row[0] for row in rows)


async def test_the_admin_listings_read_the_window_through_the_index(store, pool):
    """Newest-first runs and the oldest-first usage cohort both walk the index."""
    await _seed_history(pool)
    captured: list[Any] = []

    async def capture_queries(conn: asyncpg.Connection) -> None:
        conn.add_query_logger(captured.append)

    listing_pool = await asyncpg.create_pool(
        **_db_config(), min_size=1, max_size=1, init=capture_queries
    )
    now = datetime.now(UTC)
    since = now - timedelta(days=1)
    try:
        page = await AdminRunQueries(listing_pool).list_runs(
            filters=RunFilters(), since=since, until=now, cursor=None, limit=50
        )
        cohort, _ = await AdminUsageQueries(listing_pool).selection(
            since=since,
            until=now,
            user_id=None,
            model=None,
            runtime=None,
            repo=None,
            max_runs=_TURNS,
            max_rows=_TURNS,
            max_snapshot_bytes=1,
        )
    finally:
        await listing_pool.close()
    async with pool.acquire() as conn:
        in_window = await conn.fetchval(
            "SELECT count(*) FROM agent_jobs WHERE created_at >= $1 AND created_at < $2",
            since,
            now,
        )
    assert 50 < in_window < _TURNS
    assert len(page.runs) == 50 and page.next_cursor is not None
    assert len(cohort) == in_window

    (runs,) = [item for item in captured if "ORDER BY j.created_at DESC, j.id DESC" in item.query]
    (usage,) = [
        item
        for item in captured
        if "ORDER BY j.created_at, j.id" in item.query and "attempt_no" not in item.query
    ]
    for statement in (runs, usage):
        plan = await _plan(pool, statement)
        assert _INDEX in plan, plan
        assert "Seq Scan on agent_jobs" not in plan, plan
