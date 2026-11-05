"""Real ordered seeks, bounded payloads, and large-history allocation probes."""

from __future__ import annotations

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

from cloud_agent.api._thread_history import thread_response
from cloud_agent.domain.thread_history import THREAD_PAGE_BYTES, THREAD_PREVIEW_CHARS
from cloud_agent.storage._sql.thread_history import _entries_query
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def history(store, db, turns, content="reply"):
    root = await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_jobs(id,thread_id,user_id,parent_job_id,turn_no,native_turn_no,task_prompt,runtime,model,state) "
            "SELECT 'history_'||n,$1,$2,$3,n,n,'prompt '||n,'codex','model','succeeded' FROM generate_series(2,$4::int)n",
            root["thread_id"],
            root["user_id"],
            root["id"],
            turns,
        )
        # Interleave IDs across turns; replies are inserted before their prompts.
        await conn.execute(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content,mid_turn) "
            "SELECT thread_id,id,'assistant',$2,FALSE FROM agent_jobs WHERE thread_id=$1 ORDER BY turn_no DESC",
            root["thread_id"],
            content,
        )
        await conn.execute(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content,mid_turn) "
            "SELECT thread_id,id,'user','steering '||turn_no,TRUE FROM agent_jobs WHERE thread_id=$1 ORDER BY turn_no",
            root["thread_id"],
        )
        await conn.execute(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content,mid_turn) "
            "SELECT thread_id,id,'user',task_prompt,FALSE FROM agent_jobs WHERE thread_id=$1 AND turn_no>1 ORDER BY turn_no DESC",
            root["thread_id"],
        )
    return root


async def test_pages_preserve_interleaved_prompt_reply_and_mid_turn_order(store, queries, db):
    root = await history(store, db, 40)
    async with db.acquire() as conn:
        expected = await conn.fetch(
            "SELECT m.id FROM agent_thread_messages m JOIN agent_jobs j ON j.id=m.job_id "
            "WHERE m.thread_id=$1 ORDER BY j.turn_no,CASE WHEN m.role='user' AND NOT m.mid_turn THEN 0 ELSE 1 END,m.id",
            root["thread_id"],
        )
    cursor, chunks, jobs, visited = None, [], set(), set()
    while True:
        raw = await queries.get_thread_for_job(
            job_id=root["id"], user_id="user-1", cursor=cursor, limit=7
        )
        page = thread_response(raw)
        assert len(raw["_entries"]) <= 7
        assert len(page.model_dump_json().encode()) <= THREAD_PAGE_BYTES
        assert {m.job_id for m in page.messages} <= {j.id for j in page.jobs}
        assert page.current_job_id == "history_40"
        chunks.insert(0, [m.id for m in page.messages])
        jobs.update(j.id for j in page.jobs)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert cursor not in visited
        visited.add(cursor)
    assert [m for chunk in chunks for m in chunk] == [row["id"] for row in expected]
    assert len(jobs) == 40


async def test_empty_turn_markers_and_selected_anchor_do_not_skip_history(store, queries, db):
    root = await history(store, db, 20)
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_thread_messages WHERE thread_id=$1", root["thread_id"]
        )
    cursor, turns = None, []
    while True:
        page = thread_response(
            await queries.get_thread_for_job(
                job_id="history_12", user_id="user-1", anchor=True, cursor=cursor, limit=1
            )
        )
        turns += [j.turn_no for j in page.jobs]
        assert page.current_job_id == "history_20"
        cursor = page.next_cursor
        if cursor is None:
            break
    assert turns == list(range(12, 0, -1))


async def test_tail_pages_pick_up_new_turns_and_mid_turn_writes(store, queries, db):
    root = await history(store, db, 30)
    initial = thread_response(await queries.get_thread_for_job(job_id=root["id"], user_id="user-1"))
    async with db.acquire() as conn:
        message_id = await conn.fetchval(
            "INSERT INTO agent_thread_messages(thread_id,job_id,role,content,mid_turn) "
            "VALUES($1,'history_30','user','new steering',TRUE) RETURNING id",
            root["thread_id"],
        )
        await conn.execute(
            "INSERT INTO agent_jobs(id,thread_id,user_id,parent_job_id,turn_no,task_prompt,runtime,model) "
            "VALUES('history_31',$1,$2,'history_30',31,'new turn','codex','model')",
            root["thread_id"],
            root["user_id"],
        )
    cursor, jobs, messages = initial.sync_cursor, set(), set()
    while cursor:
        page = thread_response(
            await queries.get_thread_for_job(
                job_id=root["id"], user_id="user-1", after=cursor, limit=2
            )
        )
        jobs.update(j.turn_no for j in page.jobs)
        messages.update(m.id for m in page.messages)
        cursor = page.next_cursor
    assert jobs == {29, 30, 31}
    assert message_id in messages


async def test_large_history_bounds_queries_bytes_and_allocations(store, db, capsys):
    root = await history(store, db, 2000, '📚\\"\n' * 4096)
    logs = []
    async with db.acquire() as conn:

        @asynccontextmanager
        async def acquire():
            yield conn

        reader = PostgresConversationQueries(SimpleNamespace(acquire=acquire))
        conn.add_query_logger(logs.append)
        tracemalloc.start()
        started = time.perf_counter()
        page = thread_response(await reader.get_thread_for_job(job_id=root["id"], user_id="user-1"))
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        await asyncio.sleep(0)
        conn.remove_query_logger(logs.append)
        await conn.execute("ANALYZE agent_jobs")
        await conn.execute("ANALYZE agent_thread_messages")
        plan = await conn.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + _entries_query(False),
            root["thread_id"],
            None,
            None,
            None,
            51,
        )
        assert "idx_agent_thread_messages_job_order" in plan
        assert json.loads(plan)[0]["Plan"]["Actual Rows"] == 51
    selects = [r for r in logs if r.query.lstrip().startswith(("SELECT", "WITH"))]
    assert len(selects) == 5
    assert len(page.jobs) <= 50 and len(page.messages) <= 50
    assert page.next_cursor is not None
    size = len(page.model_dump_json().encode())
    assert size <= THREAD_PAGE_BYTES
    assert all(len(m.content) <= THREAD_PREVIEW_CHARS for m in page.messages)
    assert any(m.content_truncated for m in page.messages)
    assert peak < 16 * 1024 * 1024
    with capsys.disabled():
        print(
            json.dumps(
                {
                    "turns": 2000,
                    "messages": 6000,
                    "queries": len(selects),
                    "page_bytes": size,
                    "python_peak_bytes": peak,
                    "seconds": elapsed,
                }
            )
        )


async def test_unicode_chunks_and_cursors_keep_owner_and_deleted_scope(store, queries, db):
    root = await history(store, db, 2, "🙂ab" * 5000)
    page = thread_response(await queries.get_thread_for_job(job_id=root["id"], user_id="user-1"))
    preview = next(m for m in page.messages if m.content_truncated)
    rebuilt, offset = preview.content, preview.next_offset
    while offset is not None:
        chunk = await queries.get_thread_message_chunk(
            job_id=root["id"], user_id="user-1", message_id=preview.id, offset=offset, limit=1000
        )
        rebuilt += chunk["content"]
        offset += len(chunk["content"])
        if offset >= chunk["content_length"]:
            offset = None
    assert rebuilt == "🙂ab" * 5000
    assert (
        await queries.get_thread_message_chunk(
            job_id=root["id"], user_id="other", message_id=preview.id, offset=0, limit=1000
        )
        is None
    )
    another = await _create_job(store)
    with pytest.raises(ValueError, match="cursor"):
        await queries.get_thread_for_job(
            job_id=another["id"], user_id="user-1", cursor=page.sync_cursor
        )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at=now() WHERE id=$1", root["thread_id"]
        )
    assert await queries.get_thread_for_job(job_id=root["id"], user_id="user-1") is None
    assert (
        await queries.get_thread_message_chunk(
            job_id=root["id"], user_id="user-1", message_id=preview.id, offset=0, limit=1000
        )
        is None
    )
