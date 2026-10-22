"""Recent insights stay eligible while the indexed read avoids old history."""

import json

import pytest
from tests.integration.storage.test_agent_job_store import _create_job, pool as pool, store as store

from cloud_agent.storage.repositories.work_insights import RECENT_REQUESTS_SQL

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_projection_tracks_deleted_threads_and_mid_turn_goal_requests(store, pool):
    job = await _create_job(store, user_id="alice", task_prompt="First")
    async with pool.acquire() as conn:
        thread = await conn.fetchval("SELECT thread_id FROM agent_jobs WHERE id=$1", job["id"])
        await conn.execute("UPDATE agent_threads SET deleted_at=NOW() WHERE id=$1", thread)
        assert await conn.fetchval("SELECT count(*) FROM agent_recent_requests") == 0
        assert await store.work_insights.recent_requests("alice") == []
        await conn.execute("UPDATE agent_threads SET deleted_at=NULL WHERE id=$1", thread)
        assert await store.work_insights.recent_requests("alice") == ["First"]
        await conn.execute("UPDATE agent_jobs SET turn_source='goal' WHERE id=$1", job["id"])
        await conn.execute(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content,mid_turn) "
            "VALUES ($1,$2,'user','During goal',TRUE)",
            thread,
            job["id"],
        )
        assert await store.work_insights.recent_requests("alice") == ["During goal"]


async def test_recent_request_plan_visits_a_page_from_large_history(store, pool):
    job = await _create_job(store, user_id="alice", task_prompt="Newest")
    async with pool.acquire() as conn:
        thread = await conn.fetchval("SELECT thread_id FROM agent_jobs WHERE id=$1", job["id"])
        await conn.execute(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content,mid_turn,created_at) "
            "SELECT $1,$2,'user','Historic request ' || n,TRUE,NOW()-make_interval(secs=>n) "
            "FROM generate_series(1,100000) n",
            thread,
            job["id"],
        )
        await conn.execute(
            "ANALYZE agent_recent_requests; ANALYZE agent_thread_messages; "
            "ANALYZE agent_jobs; ANALYZE agent_threads"
        )
        plan = json.loads(
            await conn.fetchval(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + RECENT_REQUESTS_SQL, "alice", 50
            )
        )[0]["Plan"]
    nodes = []

    def visit(node):
        nodes.append(node)
        for child in node.get("Plans", []):
            visit(child)

    visit(plan)
    projected = [node for node in nodes if node.get("Relation Name") == "agent_recent_requests"]
    assert len(projected) == 1
    assert projected[0]["Index Name"] == "idx_agent_recent_requests_owner"
    assert projected[0]["Actual Rows"] <= 50
    assert len(await store.work_insights.recent_requests("alice")) == 50
