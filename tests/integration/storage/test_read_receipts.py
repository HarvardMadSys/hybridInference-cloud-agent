"""Cloud receipts survive new clients and advance monotonically under concurrent imports."""

import asyncio

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def finish(pool, job, attempt_no=1):
    """Arrange a completed attempt without involving sandbox execution."""
    async with pool.acquire() as conn:
        attempt = await conn.fetchval(
            """
            INSERT INTO agent_attempts
                (job_id, thread_id, attempt_no, lease_owner, lease_generation,
                 lease_expires_at, status, finished_at)
            VALUES ($1, $2, $3, 'test-host', 1, NOW(), 'finished', NOW()) RETURNING id
            """,
            job["id"],
            job["thread_id"],
            attempt_no,
        )
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', current_attempt_id = $2 WHERE id = $1",
            job["id"],
            attempt,
        )
    return {"job_id": job["id"], "attempt_id": attempt}


async def test_receipts_survive_new_storage_and_do_not_change_activity(pool, store):
    from cloud_agent.storage.assembly import Storage

    job = await _create_job(store)
    receipt = await finish(pool, job)
    before = await store.conversation_queries.list_conversations(user_id="user-1")
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[receipt])
    after = await store.conversation_queries.list_conversations(user_id="user-1")
    assert before == after
    new_client = Storage(pool)
    page = await new_client.conversation_queries.list_read_receipts(user_id="user-1")
    assert page["receipts"] == [{**receipt, "thread_id": job["thread_id"], "turn_no": 1}]
    assert page["next_cursor"] is None
    assert int(page["sync_cursor"]) > 0
    assert (
        await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[receipt])
        == []
    )


async def test_old_devices_and_retries_cannot_regress_read_progress(pool, store):
    first = await _create_job(store)
    old_attempt = await finish(pool, first)
    retry = await finish(pool, first, 2)
    await asyncio.gather(
        *(
            store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[receipt])
            for receipt in [retry, old_attempt, old_attempt, retry]
        )
    )
    page = await store.conversation_queries.list_read_receipts(user_id="user-1")
    assert page["receipts"][0]["attempt_id"] == retry["attempt_id"]
    second = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="next turn"
    )
    assert second is not None
    next_turn = await finish(pool, second)
    # Two receipts for one thread in the same import are reduced to the newest.
    await store.conversation_commands.merge_read_receipts(
        user_id="user-1", receipts=[next_turn, old_attempt, retry, next_turn]
    )
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[retry])
    page = await store.conversation_queries.list_read_receipts(user_id="user-1")
    assert page["receipts"] == [{**next_turn, "thread_id": second["thread_id"], "turn_no": 2}]


async def test_invalid_receipts_do_not_block_import_or_cross_owners(pool, store):
    own = await _create_job(store)
    foreign = await _create_job(store, user_id="other")
    queued = await _create_job(store)
    deleted = await _create_job(store)
    own_receipt = await finish(pool, own)
    foreign_receipt = await finish(pool, foreign)
    deleted_receipt = await finish(pool, deleted)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", deleted["thread_id"]
        )
    await store.conversation_commands.merge_read_receipts(
        user_id="user-1",
        receipts=[
            foreign_receipt,
            deleted_receipt,
            {"job_id": "missing", "attempt_id": None},
            {"job_id": queued["id"], "attempt_id": None},
            {"job_id": own["id"], "attempt_id": foreign_receipt["attempt_id"]},
            {"job_id": own["id"], "attempt_id": own_receipt["attempt_id"] + 99999},
            own_receipt,
        ],
    )
    page = await store.conversation_queries.list_read_receipts(user_id="user-1")
    assert page["receipts"] == [{**own_receipt, "thread_id": own["thread_id"], "turn_no": 1}]
    assert (await store.conversation_queries.list_read_receipts(user_id="other"))["receipts"] == []


async def test_running_retry_is_never_implicitly_marked_read(pool, store):
    job = await _create_job(store)
    old = await finish(pool, job)
    retry = await finish(pool, job, 2)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET status = 'running', finished_at = NULL WHERE id = $1",
            retry["attempt_id"],
        )
        await conn.execute("UPDATE agent_jobs SET state = 'running' WHERE id = $1", job["id"])
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[retry, old])
    page = await store.conversation_queries.list_read_receipts(user_id="user-1")
    assert page["receipts"] == [{**old, "thread_id": job["thread_id"], "turn_no": 1}]


async def test_cancelled_without_attempt_pagination_archive_and_deletion(pool, store):
    jobs = [await _create_job(store) for _ in range(3)]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'cancelled'")
        await conn.execute(
            "UPDATE agent_threads SET archived_at = NOW() WHERE id = $1", jobs[0]["thread_id"]
        )
    await store.conversation_commands.merge_read_receipts(
        user_id="user-1", receipts=[{"job_id": job["id"], "attempt_id": None} for job in jobs]
    )
    seen = []
    cursor = None
    while True:
        page = await store.conversation_queries.list_read_receipts(
            user_id="user-1", limit=1, after=int(cursor) if cursor else None
        )
        seen.extend(page["receipts"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {receipt["job_id"] for receipt in seen} == {job["id"] for job in jobs}
    assert all(receipt["attempt_id"] is None for receipt in seen)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", jobs[0]["thread_id"]
        )
    remaining = await store.conversation_queries.list_read_receipts(user_id="user-1")
    assert len(remaining["receipts"]) == 2


async def test_delta_pages_include_updates_without_replaying_unchanged_history(pool, store):
    first, second = await _create_job(store), await _create_job(store)
    old = await finish(pool, first)
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[old])
    initial = await store.conversation_queries.list_read_receipts(user_id="user-1")
    cursor = int(initial["sync_cursor"])
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[old])
    empty = await store.conversation_queries.list_read_receipts(user_id="user-1", after=cursor)
    assert empty == {"receipts": [], "next_cursor": None, "sync_cursor": str(cursor)}

    new = await finish(pool, second)
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[new])
    retry = await finish(pool, first, 2)
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[retry])
    page = await store.conversation_queries.list_read_receipts(
        user_id="user-1", after=cursor, limit=1
    )
    assert page["receipts"] == [{**new, "thread_id": second["thread_id"], "turn_no": 1}]
    assert page["next_cursor"] == page["sync_cursor"]
    last = await store.conversation_queries.list_read_receipts(
        user_id="user-1", after=int(page["next_cursor"]), limit=1
    )
    assert last["receipts"] == [{**retry, "thread_id": first["thread_id"], "turn_no": 1}]
    assert last["next_cursor"] is None
    assert int(last["sync_cursor"]) > int(page["sync_cursor"]) > cursor
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[old])
    assert (
        await store.conversation_queries.list_read_receipts(
            user_id="user-1", after=int(last["sync_cursor"])
        )
    )["receipts"] == []


async def test_polling_cannot_skip_a_receipt_that_commits_late(pool, store, monkeypatch):
    from cloud_agent.storage.repositories import conversation_commands

    first, second = await _create_job(store), await _create_job(store)
    first_receipt, second_receipt = await finish(pool, first), await finish(pool, second)
    written, release = asyncio.Event(), asyncio.Event()
    merge = conversation_commands.merge_read_receipts

    async def pause_first_commit(conn, *, user_id, receipts):
        result = await merge(conn, user_id=user_id, receipts=receipts)
        if receipts == [first_receipt]:
            written.set()
            await release.wait()
        return result

    monkeypatch.setattr(conversation_commands, "merge_read_receipts", pause_first_commit)
    first_write = asyncio.create_task(
        store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[first_receipt])
    )
    await asyncio.wait_for(written.wait(), 5)
    second_write = asyncio.create_task(
        store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[second_receipt])
    )
    try:
        # A later revision must not become visible while an earlier one is
        # uncommitted, or the next poll could permanently skip the earlier read.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_write), 0.1)
        during = await store.conversation_queries.list_read_receipts(user_id="user-1")
        assert during == {"receipts": [], "next_cursor": None, "sync_cursor": "0"}
    finally:
        release.set()
        await asyncio.gather(first_write, second_write)
    after = await store.conversation_queries.list_read_receipts(
        user_id="user-1", after=int(during["sync_cursor"])
    )
    assert [receipt["job_id"] for receipt in after["receipts"]] == [first["id"], second["id"]]


async def test_attempt_cleanup_has_an_index_and_cascades_receipts(pool, store):
    job = await _create_job(store)
    receipt = await finish(pool, job)
    await store.conversation_commands.merge_read_receipts(user_id="user-1", receipts=[receipt])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        plan = await conn.fetch(
            "EXPLAIN SELECT 1 FROM agent_conversation_read_receipts "
            "WHERE job_id = $1 AND attempt_id = $2",
            job["id"],
            receipt["attempt_id"],
        )
        assert "idx_agent_read_receipts_job_attempt" in str(plan)
        await conn.execute(
            "UPDATE agent_jobs SET current_attempt_id = NULL WHERE id = $1", job["id"]
        )
        await conn.execute("DELETE FROM agent_attempts WHERE id = $1", receipt["attempt_id"])
    assert (await store.conversation_queries.list_read_receipts(user_id="user-1"))["receipts"] == []
