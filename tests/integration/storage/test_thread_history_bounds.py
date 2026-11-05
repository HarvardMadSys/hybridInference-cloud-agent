"""JSON escaping and wide repeated fields must not defeat row-count limits."""

import pytest
from tests.integration.storage.test_conversation_queries_store import (
    db as db,
    queries as queries,
    store as store,
)
from tests.integration.storage.test_thread_history_pages import history

from cloud_agent.api._thread_history import thread_response
from cloud_agent.domain.thread_history import THREAD_PAGE_BYTES

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_byte_cutoffs_advance_without_losing_messages(store, queries, db):
    root = await history(store, db, 30)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_messages SET content=$2 WHERE thread_id=$1",
            root["thread_id"],
            "\x01" * 6000,
        )
        expected = set(
            await conn.fetch(
                "SELECT id FROM agent_thread_messages WHERE thread_id=$1", root["thread_id"]
            )
        )
    cursor, found, cuts = None, set(), 0
    while True:
        raw = await queries.get_thread_for_job(
            job_id=root["id"], user_id="user-1", cursor=cursor, limit=100
        )
        page = thread_response(raw)
        assert len(page.model_dump_json().encode()) <= THREAD_PAGE_BYTES
        ids = {message.id for message in page.messages}
        assert not ids & found
        found.update(ids)
        if len(page.messages) < len(raw["messages"]):
            cuts += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert found == {row["id"] for row in expected}
    assert cuts > 0


async def test_wide_job_fields_and_queue_snapshots_are_explicitly_incomplete(store, queries, db):
    root = await history(store, db, 2)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET task_prompt=$2,metadata=$3::jsonb WHERE thread_id=$1",
            root["thread_id"],
            "x" * 100000,
            '{"large":"' + "x" * 20000 + '"}',
        )
        await conn.execute(
            "INSERT INTO agent_pending_messages(id,thread_id,user_id,state,prompt) VALUES('pending',$1,$2,'queued',$3)",
            root["thread_id"],
            root["user_id"],
            "p" * 100000,
        )
    page = thread_response(await queries.get_thread_for_job(job_id=root["id"], user_id="user-1"))
    assert all(job.thread_preview for job in page.jobs)
    assert all(len(job.task_prompt) == 4096 for job in page.jobs)
    assert page.pending_messages_complete is False
    assert page.pending_messages == []
    assert len(page.model_dump_json().encode()) <= THREAD_PAGE_BYTES
    # No preview replaced the editable source of truth.
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT length(prompt) FROM agent_pending_messages WHERE id='pending'"
            )
            == 100000
        )
