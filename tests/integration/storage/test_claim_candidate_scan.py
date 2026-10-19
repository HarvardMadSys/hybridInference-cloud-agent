"""A claim reads the retained sandbox of the turn it hands out, and no other.

Every idle host claims again whenever a turn is queued, requeued or ends, and
each claim walks the queued turns oldest first until one is runnable here. The
sandbox a claim returns -- its publication state, its members, whether its host
can fork it -- takes a dozen correlated reads, and was built for every queued
turn the walk passed: a backlog of follow-ups held for other hosts made each
claim, on every host, pay for all of them. The plan check runs the statement
the claim actually sends, captured on the wire.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config

from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: Follow-ups queued for groups that live on another host.
_HELD = 40


async def _queue_follow_ups_elsewhere(pool: Any, count: int) -> None:
    """``count`` chats whose second turn waits for the host that holds their group."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads
                (id, user_id, repo, title, title_source, setup_script, execution_profile)
            SELECT 'athr_held_' || g, 'user-held', 'example-org/example-repo',
                   'held', 'fallback', NULL, 'trusted-v1'
            FROM generate_series(1, $1) g
            """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs
                (id, thread_id, user_id, parent_job_id, turn_no, native_turn_no,
                 task_prompt, runtime, model, state, created_at)
            SELECT 'ajob_held_' || g || '_' || turn, 'athr_held_' || g, 'user-held',
                   CASE WHEN turn = 2 THEN 'ajob_held_' || g || '_1' END, turn,
                   CASE WHEN turn = 1 THEN 1 END, 'p', 'claude-code', 'glm-5.1',
                   CASE WHEN turn = 1 THEN 'succeeded' ELSE 'queued' END,
                   NOW() - make_interval(mins => 2 * ($1 - g) + 2 - turn)
            FROM generate_series(1, $1) g CROSS JOIN generate_series(1, 2) turn
            """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_sandboxes
                (id, user_id, origin_thread_id, host, container_id, container_name,
                 runtime_id, execution_profile, state)
            SELECT 'asbx_held_' || g, 'user-held', 'athr_held_' || g, 'other-runner',
                   'ctr_held_' || g, 'held-' || g, 'claude-code', 'trusted-v1', 'stopped'
            FROM generate_series(1, $1) g
            """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships
                (thread_id, user_id, sandbox_id, native_session_id, native_state,
                 context_status, last_settled_job_id)
            SELECT 'athr_held_' || g, 'user-held', 'asbx_held_' || g, 'native-' || g,
                   'ready', 'live', 'ajob_held_' || g || '_1'
            FROM generate_series(1, $1) g
            """,
            count,
        )
        for table in (
            "agent_threads",
            "agent_jobs",
            "agent_sandboxes",
            "agent_thread_sandbox_memberships",
        ):
            await conn.execute(f"ANALYZE {table}")


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node of one ``EXPLAIN (FORMAT JSON)`` plan, subplans included."""
    return [node, *(item for child in node.get("Plans", []) for item in _plan_nodes(child))]


async def test_a_claim_reads_no_sandbox_of_the_turns_it_passes(store, pool):
    """Turns held for another host leave this host's claim without reading their groups."""
    await _queue_follow_ups_elsewhere(pool, _HELD)
    captured: list[Any] = []

    async def capture_queries(conn: asyncpg.Connection) -> None:
        conn.add_query_logger(captured.append)

    claim_pool = await asyncpg.create_pool(
        **_db_config(), min_size=1, max_size=1, init=capture_queries
    )
    try:
        claimed = await _claim(Storage(claim_pool), worker_id="w1", lease_ttl_seconds=60)
    finally:
        await claim_pool.close()
    # Every queued turn is a follow-up of a group another host holds.
    assert claimed is None

    (statement,) = [item for item in captured if "FOR UPDATE OF j SKIP LOCKED" in item.query]
    async with pool.acquire() as conn:
        # Run again exactly as sent; it locks nothing it would keep.
        transaction = conn.transaction()
        await transaction.start()
        try:
            plan = await conn.fetchval(
                f"EXPLAIN (ANALYZE, FORMAT JSON) {statement.query}", *statement.args
            )
        finally:
            await transaction.rollback()
    reads = [
        node["Actual Loops"]
        for node in _plan_nodes(json.loads(plan)[0]["Plan"])
        if node.get("Relation Name") == "agent_sandbox_snapshots"
    ]
    assert reads, "the claim no longer reads any group's snapshots"
    assert max(reads) <= 1, f"the claim read the snapshots of {max(reads)} groups"
