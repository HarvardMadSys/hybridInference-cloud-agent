"""Real PostgreSQL replay cursors and canonical host/manifest projections."""

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    _reserve_retained_chat,
    pool as pool,
    store as store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_tied_timestamps_page_past_failed_prefix_even_after_cursor_is_cleared(pool, store):
    jobs = [await _create_job(store) for _ in range(101)]
    ids = sorted(job["id"] for job in jobs)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'cancelled', terminal_resume_pending = TRUE, "
            "updated_at = '2026-01-01T00:00:00Z' WHERE id = ANY($1::text[])",
            ids,
        )
    page = await store.sandbox.terminal_resume_page()
    assert [row["id"] for row in page] == ids[:100]
    assert all(row["thread_sandbox"] is None for row in page)
    cursor = (page[-1]["updated_at"], page[-1]["id"])
    assert await store.sandbox.mark_terminal_resume_complete(job_id=cursor[1])
    tail = await store.sandbox.terminal_resume_page(after=cursor)
    assert [row["id"] for row in tail] == ids[100:]
    assert (
        await store.sandbox.terminal_resume_page(after=(tail[0]["updated_at"], tail[0]["id"])) == []
    )


async def test_replay_reads_same_current_owner_and_manifest_without_job_payload(pool, store):
    job, claim, sandbox = await _reserve_retained_chat(store)
    await store.host_pool.register_host(host="moved-host", credential_hash="new-host-hash")
    await store.host_pool.record_host_heartbeat(
        host="moved-host", sandbox_manager_url="http://moved-host:9101"
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() WHERE id = $1",
            claim["attempt_id"],
        )
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', terminal_resume_pending = TRUE WHERE id = $1",
            job["id"],
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET host = 'moved-host' WHERE id = $1", sandbox["sandbox_id"]
        )
    [row] = await store.sandbox.terminal_resume_page()
    full = await store.conversation_queries.get_job(job["id"])
    owner = await store.host_pool.host_for_job(job_id=job["id"])
    assert row["thread_sandbox"] == full["thread_sandbox"]
    assert row["host"] == owner["host"] == "moved-host"
    assert row["sandbox_manager_url"] == owner["sandbox_manager_url"] == "http://moved-host:9101"
    assert set(row) == {
        "id",
        "thread_id",
        "updated_at",
        "thread_sandbox",
        "host",
        "sandbox_manager_url",
    }
    # A marked row that has become nonterminal cannot be replayed or cleared.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'queued' WHERE id = $1", job["id"])
    assert await store.sandbox.terminal_resume_page() == []
    assert not await store.sandbox.mark_terminal_resume_complete(job_id=job["id"])
