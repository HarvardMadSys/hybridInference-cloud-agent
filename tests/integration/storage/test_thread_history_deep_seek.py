"""A cursor deep inside one long turn must seek, not discard thousands of rows."""

import json

import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_conversation_queries_store import db as db, store as store

from cloud_agent.storage._sql.thread_history import _entries_query

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def nodes(plan):
    return [plan, *(node for child in plan.get("Plans", []) for node in nodes(child))]


@pytest.mark.parametrize("forward", [False, True])
async def test_deep_message_cursor_is_an_index_bound_not_a_filter(store, db, forward):
    job = await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content) "
            "SELECT $1,$2,'assistant','reply '||n FROM generate_series(1,10000)n",
            job["thread_id"],
            job["id"],
        )
        cursor = await conn.fetchval(
            "SELECT id FROM agent_thread_messages WHERE job_id=$1 ORDER BY id LIMIT 1 OFFSET 5000",
            job["id"],
        )
        await conn.execute("ANALYZE agent_thread_messages")
        raw = await conn.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + _entries_query(forward),
            job["thread_id"],
            1,
            1,
            cursor,
            6,
        )
    plan = json.loads(raw)[0]["Plan"]
    seeks = [
        node
        for node in nodes(plan)
        if node.get("Index Name") == "idx_agent_thread_messages_job_order"
    ]
    assert seeks
    assert all("ROW(" in node["Index Cond"] for node in seeks)
    assert all(node.get("Rows Removed by Filter", 0) == 0 for node in seeks)
    assert all(node["Actual Rows"] <= 6 for node in seeks)
    assert plan["Actual Rows"] == 6
