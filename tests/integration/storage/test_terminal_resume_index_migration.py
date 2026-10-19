"""The terminal-resume replay finds its few marked turns through an index.

Every maintenance pass, every thirty seconds, asks for the settled turns whose
terminals still wait to be resumed. Nothing indexed the marker, so each pass
read the whole job history to find a handful of rows. The plan check runs the
statement the replay actually sends, captured on the wire, so a later edit
that stops the index from serving it fails here.
"""

from __future__ import annotations

import json
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

from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0084_host_removal_move_out"
_THIS = "0085_terminal_resume_index"
_INDEX = "idx_agent_jobs_terminal_resume"
#: Enough history that reading all of it is the plan to avoid.
_TURNS = 5000
#: Settled turns still waiting for their terminals, oldest first.
_MARKED = ["ajob_hist_4000", "ajob_hist_2000", "ajob_hist_10"]


async def _index(pool) -> asyncpg.Record | None:
    """Whether the index exists, whether it is valid, and what it covers."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition "
            "FROM pg_index i WHERE i.indexrelid = to_regclass($1)",
            _INDEX,
        )


async def test_an_interrupted_concurrent_build_is_replaced_by_a_valid_index(store, pool):
    """A concurrent build that failed leaves an invalid index; the upgrade rebuilds it."""
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
        assert built["definition"].endswith(
            "USING btree (updated_at) WHERE terminal_resume_pending"
        )

        downgrade_to(config, _BEFORE)
        assert await _index(pool) is None
    finally:
        apply_migrations(config)


async def _seed_history(pool) -> None:
    """``_TURNS`` settled single-turn conversations; ``_MARKED`` still wait to resume."""
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
                 native_turn_no, terminal_resume_pending, created_at, updated_at)
            SELECT 'ajob_hist_' || g, 'athr_hist_' || g, 'user-' || (g % 50), 1, 'p',
                   'claude-code', 'glm-5.1', 'succeeded', 1,
                   ('ajob_hist_' || g) = ANY($2::text[]),
                   NOW() - make_interval(mins => g), NOW() - make_interval(mins => g)
            FROM generate_series(1, $1) g
            """,
            _TURNS,
            _MARKED,
        )
        await conn.execute("ANALYZE agent_jobs")


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node of one ``EXPLAIN (FORMAT JSON)`` plan, subplans included."""
    return [node, *(item for child in node.get("Plans", []) for item in _plan_nodes(child))]


async def test_the_resume_replay_reads_only_the_marked_turns(store, pool):
    """The replay walks the marked turns oldest first, and reads nothing else."""
    await _seed_history(pool)
    captured: list[Any] = []

    async def capture_queries(conn: asyncpg.Connection) -> None:
        conn.add_query_logger(captured.append)

    replay_pool = await asyncpg.create_pool(
        **_db_config(), min_size=1, max_size=1, init=capture_queries
    )
    try:
        pending = await Storage(replay_pool).sandbox.list_terminal_resumes_pending()
    finally:
        await replay_pool.close()
    assert pending == _MARKED

    (statement,) = [item for item in captured if "terminal_resume_pending" in item.query]
    async with pool.acquire() as conn:
        plan = await conn.fetchval(
            f"EXPLAIN (ANALYZE, FORMAT JSON) {statement.query}", *statement.args
        )
    read = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _plan_nodes(json.loads(plan)[0]["Plan"])
        if node.get("Relation Name") == "agent_jobs"
    )
    assert read <= len(_MARKED), f"read {read} turns to find {len(_MARKED)}"
