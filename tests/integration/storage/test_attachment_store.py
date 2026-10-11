"""Real Postgres quotas, ownership, claiming, and lifetime for uploads."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _expire_attempt,
    _settled,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)

from cloud_agent.attachments.models import (
    MAX_ATTACHMENT_FILE_BYTES,
    AttachmentError,
    AttachmentLeaseLost,
)
from cloud_agent.attachments.selection import resolve_attachments

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def stage(store, path="notes.md", *, user_id="user-1", content=b"hello"):
    return await store.attachments.create_attachment(
        user_id=user_id, path=path, content=content, content_type="text/plain"
    )


async def test_a_staged_upload_reports_its_size_and_never_its_bytes(store):
    row = await stage(store, content=b"hello world")
    assert row["size_bytes"] == 11
    assert "content" not in row
    assert row["job_id"] is None


async def test_uploads_are_scoped_to_their_owner(store):
    mine = await stage(store)
    assert (
        await store.attachments.select_attachments(user_id="user-2", attachment_ids=[mine["id"]])
        == []
    )
    assert not await store.attachments.delete_attachment(user_id="user-2", attachment_id=mine["id"])
    assert await store.attachments.delete_attachment(user_id="user-1", attachment_id=mine["id"])


async def test_an_oversized_file_is_refused_by_the_store_as_well(store):
    with pytest.raises(AttachmentError):
        await stage(store, content=b"x" * (MAX_ATTACHMENT_FILE_BYTES + 1))
    with pytest.raises(AttachmentError):
        await stage(store, content=b"")


async def test_the_staging_quota_counts_bytes_not_only_files(store):
    # Four files of 5 MB fit the 20 MB total; a fifth does not.
    for index in range(4):
        await stage(store, f"{index}.bin", content=b"x" * MAX_ATTACHMENT_FILE_BYTES)
    with pytest.raises(AttachmentError):
        await stage(store, "5.bin", content=b"x" * MAX_ATTACHMENT_FILE_BYTES)


async def test_creating_a_job_binds_its_uploads_and_frees_the_staging_quota(store):
    staged = await stage(store)
    manifest = await resolve_attachments(
        store.attachments, user_id="user-1", attachment_ids=[staged["id"]]
    )
    job = await _create_job(store, attachments=manifest)

    # Bound to the turn, so it is no longer selectable for another one.
    assert (
        await store.attachments.select_attachments(user_id="user-1", attachment_ids=[staged["id"]])
        == []
    )
    rows = await store.attachments.list_thread_attachments(thread_id=job["thread_id"])
    assert [row["path"] for row in rows] == ["notes.md"]
    assert await store.attachments.count_thread_attachments(thread_id=job["thread_id"]) == 1
    assert job["attachments"] == [
        {"id": staged["id"], "path": "notes.md", "size_bytes": 5, "content_type": "text/plain"}
    ]


async def test_a_second_submission_cannot_take_uploads_a_job_already_holds(store):
    """Both resolve, both create — and the loser must not keep a broken job."""
    staged = await stage(store)
    manifest = await resolve_attachments(
        store.attachments, user_id="user-1", attachment_ids=[staged["id"]]
    )
    await _create_job(store, attachments=manifest)
    with pytest.raises(AttachmentError):
        await _create_job(store, attachments=manifest)


async def test_the_host_reads_a_claimed_upload_by_its_thread(store):
    staged = await stage(store, content=b"payload")
    manifest = await resolve_attachments(
        store.attachments, user_id="user-1", attachment_ids=[staged["id"]]
    )
    job = await _create_job(store, attachments=manifest)

    found = await store.attachments.read_attachment_content(
        thread_id=job["thread_id"], attachment_id=staged["id"]
    )
    assert found == (b"payload", "text/plain")
    # A token for another conversation reads nothing.
    assert (
        await store.attachments.read_attachment_content(
            thread_id="other-thread", attachment_id=staged["id"]
        )
        is None
    )


async def test_a_follow_ups_uploads_join_the_same_conversation(store):
    first = await stage(store, "one.md")
    job = await _create_job(
        store,
        attachments=await resolve_attachments(
            store.attachments, user_id="user-1", attachment_ids=[first["id"]]
        ),
    )
    second = await stage(store, "two.md")
    await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id="user-1",
        prompt="and now this",
        attachments=await resolve_attachments(
            store.attachments, user_id="user-1", attachment_ids=[second["id"]]
        ),
    )
    rows = await store.attachments.list_thread_attachments(thread_id=job["thread_id"])
    assert [row["path"] for row in rows] == ["one.md", "two.md"]


async def test_deleting_the_conversation_takes_its_uploads_with_it(store, pool):
    """The owner delete is a *soft* one, so the cascade alone is not enough.

    ``delete_sessions`` deliberately keeps the job, residency and sandbox
    ledgers, which means the foreign key's cascade never fires. Without an
    explicit delete the bytes would outlive the conversation forever, so that
    is what this asserts.
    """
    staged = await stage(store)
    job = await _create_job(
        store,
        attachments=await resolve_attachments(
            store.attachments, user_id="user-1", attachment_ids=[staged["id"]]
        ),
    )
    async with pool.acquire() as conn:
        # A settled turn: the state and the native turn number move together,
        # which is what ck_agent_jobs_succeeded_has_native_turn requires.
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE thread_id = $1",
            job["thread_id"],
        )
        await conn.execute(
            "UPDATE agent_threads SET archived_at = NOW() WHERE id = $1", job["thread_id"]
        )
    result = await store.conversation_commands.delete_sessions(
        thread_ids=[job["thread_id"]], user_id="user-1"
    )
    assert "error" not in result, result

    async with pool.acquire() as conn:
        # The ledger survives; the owner's own bytes do not.
        assert await conn.fetchval("SELECT count(*) FROM agent_jobs WHERE id = $1", job["id"]) == 1
        remaining = await conn.fetchval(
            "SELECT count(*) FROM agent_job_attachments WHERE id = $1", staged["id"]
        )
    assert remaining == 0


async def test_a_hard_delete_of_the_job_row_still_cascades(store, pool):
    """The second path: nothing else has to remember to clean up after it."""
    staged = await stage(store)
    job = await _create_job(
        store,
        attachments=await resolve_attachments(
            store.attachments, user_id="user-1", attachment_ids=[staged["id"]]
        ),
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_thread_messages WHERE thread_id = $1", job["thread_id"]
        )
        await conn.execute("DELETE FROM agent_jobs WHERE id = $1", job["id"])
        remaining = await conn.fetchval(
            "SELECT count(*) FROM agent_job_attachments WHERE id = $1", staged["id"]
        )
    assert remaining == 0


async def test_uploads_no_task_ever_claimed_are_collected(store, pool):
    staged = await stage(store)
    assert await store.attachments.purge_stale_staged(older_than_hours=24) == 0
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_job_attachments SET created_at = NOW() - INTERVAL '48 hours' "
            "WHERE id = $1",
            staged["id"],
        )
    assert await store.attachments.purge_stale_staged(older_than_hours=24) == 1
    assert (
        await store.attachments.select_attachments(user_id="user-1", attachment_ids=[staged["id"]])
        == []
    )


async def claimed_upload(store):
    """Bind an upload and claim it through the production enrollment contract."""
    upload = await stage(store, content=b"payload")
    job = await _create_job(
        store,
        attachments=await resolve_attachments(
            store.attachments, user_id="user-1", attachment_ids=[upload["id"]]
        ),
    )
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    return job, upload, fence


async def assert_worker_reads_rejected(store, fence, attachment_id):
    """Neither a manifest nor bytes remain readable after authority is lost."""
    with pytest.raises(AttachmentLeaseLost):
        await store.attachments.list_worker_attachments(**fence)
    with pytest.raises(AttachmentLeaseLost):
        await store.attachments.read_worker_attachment(**fence, attachment_id=attachment_id)


async def test_a_current_runner_can_restore_uploads_and_drain_without_losing_reads(store):
    """Draining refuses new claims while allowing the current turn to finish."""
    _job, upload, fence = await claimed_upload(store)
    assert await store.host_pool.set_host_status(host="runner-a", status="draining") == "ok"
    rows = await store.attachments.list_worker_attachments(**fence)
    assert [row["id"] for row in rows] == [upload["id"]]
    assert "content" not in rows[0]
    assert await store.attachments.read_worker_attachment(**fence, attachment_id=upload["id"]) == (
        "notes.md",
        b"payload",
        "text/plain",
    )
    foreign = await stage(store, user_id="user-2", path="foreign.txt", content=b"private")
    assert (
        await store.attachments.read_worker_attachment(**fence, attachment_id=foreign["id"]) is None
    )


async def test_a_current_runner_without_uploads_gets_an_empty_manifest(store):
    """An empty conversation differs from an unauthorized attempt."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await store.attachments.list_worker_attachments(**fence) == []
    assert await store.attachments.read_worker_attachment(**fence, attachment_id=123456) is None


async def test_a_finished_runner_cannot_read_later_conversation_uploads(store, pool):
    """A former turn's signed token must not expose uploads to later turns."""
    job, _upload, fence = await claimed_upload(store)
    await _settled(pool, store, job["id"])
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )
    later = await stage(store, path="later.txt", content=b"later private upload")
    await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id="user-1",
        prompt="now read this",
        attachments=await resolve_attachments(
            store.attachments, user_id="user-1", attachment_ids=[later["id"]]
        ),
    )
    await assert_worker_reads_rejected(store, fence, later["id"])


async def test_an_expired_runner_is_refused_before_the_reaper_runs(store, pool):
    """Token parsing and a still-running status cannot extend an expired lease."""
    _job, upload, fence = await claimed_upload(store)
    await _expire_attempt(pool, store, fence["attempt_id"])
    await assert_worker_reads_rejected(store, fence, upload["id"])


async def test_a_superseded_runner_is_refused_while_the_new_attempt_can_restore(store, pool):
    """Retry recovery keeps thread-wide reads while rejecting the previous token."""
    job, upload, fence = await claimed_upload(store)
    await _expire_attempt(pool, store, fence["attempt_id"])
    await store.attempt_commands.reap_expired()
    replacement = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
    assert replacement is not None
    current = {
        "job_id": job["id"],
        "attempt_id": replacement["attempt_id"],
        "lease_generation": replacement["lease_generation"],
    }
    assert current["attempt_id"] != fence["attempt_id"]
    await assert_worker_reads_rejected(store, fence, upload["id"])
    assert await store.attachments.read_worker_attachment(
        **current, attachment_id=upload["id"]
    ) == ("notes.md", b"payload", "text/plain")


@pytest.mark.parametrize("change", ["revoked", "disabled", "unassigned"])
async def test_a_live_attempt_does_not_outlast_its_host_authority(store, pool, change):
    """Even an unexpired current attempt needs an enrolled, authorized host."""
    _job, upload, fence = await claimed_upload(store)
    async with pool.acquire() as conn:
        if change == "revoked":
            await conn.execute(
                "UPDATE agent_runner_hosts SET credential_hash = NULL WHERE host = 'runner-a'"
            )
        elif change == "disabled":
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = 'disabled' WHERE host = 'runner-a'"
            )
        else:
            await conn.execute(
                "UPDATE agent_attempts SET host_id = NULL WHERE id = $1", fence["attempt_id"]
            )
    await assert_worker_reads_rejected(store, fence, upload["id"])


@pytest.mark.parametrize("change", ["job", "generation", "current_attempt"])
async def test_the_worker_fence_must_match_every_current_claim_component(store, pool, change):
    """A live attempt id alone grants no authority over another job or generation."""
    job, upload, fence = await claimed_upload(store)
    if change == "job":
        other = await _create_job(store)
        fence["job_id"] = other["id"]
    elif change == "generation":
        fence["lease_generation"] += 1
    else:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET current_attempt_id = NULL WHERE id = $1", job["id"]
            )
    await assert_worker_reads_rejected(store, fence, upload["id"])
