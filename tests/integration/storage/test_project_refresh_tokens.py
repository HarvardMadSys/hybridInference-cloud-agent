"""One grouped read detects every changed project without collecting row bodies."""

import asyncio
import json
import time
import tracemalloc
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_conversation_queries_store import (
    db as db,
    queries as queries,
    store as store,
)

from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def tokens(queries, owner="user-1", archived=False, field="refresh_token"):
    return {
        row["repo"]: row[field]
        for row in await queries.list_projects(user_id=owner, archived=archived)
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE agent_jobs SET state='cancelled' WHERE id=$1",
        "UPDATE agent_jobs SET published_pr_url='https://example.test/pr/1' WHERE id=$1",
        "UPDATE agent_jobs SET metadata='{}'::jsonb || '{\"changed\":true}'::jsonb WHERE id=$1",
        "UPDATE agent_threads SET title='renamed' WHERE id=$2",
        "UPDATE agent_threads SET pinned_at=NOW() WHERE id=$2",
        "UPDATE agent_threads SET archived_at=NOW() WHERE id=$2",
        "UPDATE agent_threads SET deleted_at=NOW() WHERE id=$2",
        "UPDATE agent_threads SET repo='org/moved' WHERE id=$2",
    ],
)
async def test_project_token_detects_nonmaximal_and_membership_mutations(
    store, queries, db, mutation
):
    changed = await _create_job(store, repo="org/changed")
    newest = await _create_job(store, repo="org/changed")
    await _create_job(store, repo="org/unchanged")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET updated_at='2099-01-01' WHERE id=$1", newest["id"]
        )
    before = await tokens(queries)
    members_before = await tokens(queries, field="membership_token")
    assert before == await tokens(queries)
    async with db.acquire() as conn:
        # Give every statement the same typed arguments, even if it uses only one.
        await conn.execute(
            "WITH args AS (SELECT $1::text,$2::text) " + mutation,
            changed["id"],
            changed["thread_id"],
        )
    after = await tokens(queries)
    assert after["org/changed"] != before["org/changed"]
    assert after["org/unchanged"] == before["org/unchanged"]
    assert all(len(value) == 128 for value in after.values())
    members_after = await tokens(queries, field="membership_token")
    if any(column in mutation for column in ("archived_at", "deleted_at", "repo=")):
        assert members_after["org/changed"] != members_before["org/changed"]
    else:
        assert members_after == members_before


async def test_tokens_stay_owner_scoped_and_cover_same_count_replacements(store, queries, db):
    mine = await _create_job(store, repo="org/shared")
    theirs = await _create_job(store, repo="org/shared", user_id="user-2")
    before = await tokens(queries)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET detail='other owner' WHERE id=$1", theirs["id"])
    assert await tokens(queries) == before
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at=NOW() WHERE id=$1", mine["thread_id"]
        )
    await _create_job(store, repo="org/shared")
    assert (await queries.list_projects(user_id="user-1"))[0]["task_count"] == 1
    assert await tokens(queries) != before


async def test_tokens_use_one_grouped_query_for_twenty_long_projects(store, db, capsys):
    for n in range(20):
        await _create_job(store, repo=f"org/repo-{n}")
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO agent_jobs(id,thread_id,user_id,parent_job_id,turn_no,task_prompt,runtime,model,state)
            SELECT 'history_'||t.id||'_'||n,t.id,t.user_id,j.id,n,'prompt','codex','model','cancelled'
            FROM agent_threads t JOIN agent_jobs j ON j.thread_id=t.id AND j.turn_no=1
            CROSS JOIN generate_series(2,501)n
        """)
        logs = []

        @asynccontextmanager
        async def acquire():
            yield conn

        reader = PostgresConversationQueries(SimpleNamespace(acquire=acquire))
        conn.add_query_logger(logs.append)
        tracemalloc.start()
        started = time.perf_counter()
        result = await reader.list_projects(user_id="user-1")
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        await asyncio.sleep(0)
        conn.remove_query_logger(logs.append)
        assert len(logs) == 1
        measured = json.loads(
            await conn.fetchval("EXPLAIN (ANALYZE, FORMAT JSON) " + logs[0].query, *logs[0].args)
        )[0]
        plan = measured["Plan"]
        # Keep the pre-change aggregate as a comparison, not a flaky time gate.
        baseline_sql = logs[0].query.replace(
            ",\n                       bit_xor(('x' || md5(ROW(j.id, j.revision, t.id, t.revision)::text))::bit(128))::text AS refresh_token,"
            "\n                       bit_xor(('x' || md5(ROW(j.id, t.id)::text))::bit(128))::text AS membership_token",
            "",
        )
        assert baseline_sql != logs[0].query
        baseline = json.loads(
            await conn.fetchval("EXPLAIN (ANALYZE, FORMAT JSON) " + baseline_sql, *logs[0].args)
        )[0]
    assert len(result) == 20
    assert all(len(row["refresh_token"]) == 128 for row in result)
    assert peak < 1024 * 1024
    assert plan["Actual Rows"] == 20
    with capsys.disabled():
        print(
            json.dumps(
                {
                    "projects": 20,
                    "jobs": 10020,
                    "queries": len(logs),
                    "python_peak_bytes": peak,
                    "seconds": elapsed,
                    "database_ms": measured["Execution Time"],
                    "baseline_database_ms": baseline["Execution Time"],
                }
            )
        )


async def test_equal_count_repository_swap_changes_both_memberships(store, queries, db):
    left = await _create_job(store, repo="org/left")
    right = await _create_job(store, repo="org/right")
    before = await tokens(queries, field="membership_token")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET repo=CASE WHEN id=$1 THEN 'org/right' ELSE 'org/left' END WHERE id=ANY($2::text[])",
            left["thread_id"],
            [left["thread_id"], right["thread_id"]],
        )
    after = await tokens(queries, field="membership_token")
    assert set(after) == set(before)
    assert all(after[repo] != before[repo] for repo in before)
    assert all(row["task_count"] == 1 for row in await queries.list_projects(user_id="user-1"))
