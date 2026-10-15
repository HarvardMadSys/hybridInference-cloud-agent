"""Real Postgres merge-check fencing, current-session mapping and archive receipts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

# Imported fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _claim,
    _create_job,
    _settled,
    pool,
    store,
)

from cloud_agent.goals.models import GoalInput
from cloud_agent.publish_worker import GitHubCredential
from cloud_agent.storage import repositories
from cloud_agent.workers.pr_archive import poll_pr_merge_archives

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PR_URL = "https://github.com/example-org/example-repo/pull/42"
MERGED_AT = datetime(2026, 10, 10, 12, tzinfo=UTC)


async def _published_job(pool, store, *, pr_url=PR_URL, retain=False, **overrides):
    """Finish a real claimed turn and retain its original publisher mapping.

    Legacy original jobs retain PR URLs without snapshot membership. Fork
    copies retain the URL as display history, which does not associate them.
    """
    job = await _create_job(store, **overrides)
    claim = await _claim(store, worker_id=job["id"], lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == job["id"]
    if retain:
        sandbox = await store.sandbox.reserve_thread_sandbox(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="runner-a",
            sandbox_id=f"asbx_merge_{job['thread_id']}",
            container_name=f"merge-{job['thread_id']}",
            runtime_id=job["runtime"],
            runtime_version=claim.get("runtime_version"),
            sandbox_image_digest=claim.get("sandbox_image_digest"),
            execution_profile=job["execution_profile"],
        )
        assert sandbox is not None
        fence = {
            "thread_id": job["thread_id"],
            "attempt_id": claim["attempt_id"],
            "lease_generation": claim["lease_generation"],
            "sandbox_generation": sandbox["generation"],
            "activation_generation": sandbox["activation_generation"],
        }
        assert await store.sandbox.mark_thread_sandbox_running(
            **fence, container_id=f"merge-container-{job['thread_id']}", context_status="new"
        )
        residency = await store.residency.establish_residency(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="runner-a",
            sandbox_id=sandbox["sandbox_id"],
            runtime_id=sandbox["runtime_id"],
            runtime_version=sandbox["runtime_version"],
            ttl_seconds=1800,
        )
        assert residency is not None
        assert await store.sandbox.admit_thread_sandbox_turn(
            **fence, residency_generation=residency["generation"], job_id=job["id"]
        )
        assert await store.sandbox.settle_thread_sandbox(
            **fence,
            residency_generation=residency["generation"],
            job_id=job["id"],
            context_status="live",
            native_session_id=f"merge-native-{job['thread_id']}",
            idle_timeout_seconds=1800,
            base_sha=job["base_sha"],
        )
    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "The change is ready for review."},
    )
    await _settled(pool, store, job["id"])
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET published_pr_url = $2 WHERE id = $1", job["id"], pr_url
        )
    return job


async def _finish(store, check, **overrides):
    """Complete the exact identity and claim returned by the repository."""
    params = {
        key: check[key] for key in ("thread_id", "job_id", "user_id", "pr_url", "claim_token")
    }
    params.update(merged_at=MERGED_AT, merge_sha="a" * 40)
    params.update(overrides)
    return await store.pr_archive.complete_pr_merge_archive(**params)


async def _release(store, check, **overrides):
    """Release a check without extending any expired or superseded fence."""
    params = {key: check[key] for key in ("thread_id", "pr_url", "claim_token")}
    params.update(delay_s=0)
    params.update(overrides)
    return await store.pr_archive.reschedule_pr_archive_check(**params)


# What the worker passes with the default settings: a check is skipped after
# ten failures, then tried again 56 times.
ACCESS_RECOVERY_FAILURES = 10 + 56


async def _skip_for_access(pool, store, *, error_code="github_access_denied"):
    """Fail the only check until it is skipped; return its last claim."""
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    for attempt in range(10):
        await _release(store, check, error_code=error_code, terminal=attempt == 9)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE agent_pr_archive_checks SET next_check_at = NOW()")
        if attempt < 9:
            check = (await store.pr_archive.claim_pr_archive_checks())[0]
    return check


async def _recover(store):
    """Claim as the worker does, skipped access failures included."""
    return await store.pr_archive.claim_pr_archive_checks(
        access_recovery_failures=ACCESS_RECOVERY_FAILURES
    )


async def test_merge_archives_only_associated_sessions_and_records_durable_receipt(pool, store):
    first = await _published_job(pool, store)
    second = await _published_job(pool, store)
    unrelated = await _published_job(pool, store, pr_url=PR_URL.replace("42", "99"))
    checks = await store.pr_archive.claim_pr_archive_checks()
    merged = [check for check in checks if check["pr_url"] == PR_URL]
    assert {check["thread_id"] for check in merged} == {first["thread_id"], second["thread_id"]}
    async with pool.acquire() as conn:
        transcript_before = await conn.fetch("SELECT * FROM agent_thread_messages ORDER BY id")
    for check in merged:
        receipt = await _finish(store, check)
        assert receipt is not None and receipt["thread_id"] == check["thread_id"]
        assert receipt["archived_at"] is not None
        assert await _finish(store, check) is None
    async with pool.acquire() as conn:
        assert (
            await conn.fetch("SELECT * FROM agent_thread_messages ORDER BY id") == transcript_before
        )
        assert (
            await conn.fetchval(
                "SELECT archived_at FROM agent_threads WHERE id = $1", unrelated["thread_id"]
            )
            is None
        )
        receipts = await conn.fetch(
            "SELECT * FROM agent_pr_archive_checks WHERE state = 'archived'"
        )
        assert len(receipts) == 2
        assert all(
            row["merged_at"] == MERGED_AT and row["merge_sha"] == "a" * 40 for row in receipts
        )
        assert all(
            row["claim_token"] is None and row["completed_at"] is not None for row in receipts
        )
        events = await conn.fetch(
            "SELECT job_id, payload FROM agent_job_events "
            "WHERE payload->>'phase' = 'session_archived_pr_merged'"
        )
        assert {event["job_id"] for event in events} == {first["id"], second["id"]}


async def test_latest_unpublished_turn_prevents_discovery_and_racing_archive(pool, store):
    published = await _published_job(pool, store, retain=True)
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    newer = await store.conversation_commands.create_follow_up(
        parent_job_id=published["id"], user_id=published["user_id"], prompt="Start the next change"
    )
    assert newer is not None and "error" not in newer
    # The original has more updates, but turn_no determines the current tip.
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT revision FROM agent_jobs WHERE id = $1", published["id"]
        ) > await conn.fetchval("SELECT revision FROM agent_jobs WHERE id = $1", newer["id"])
    assert await _finish(store, check) is None
    assert await _release(store, check)
    assert await store.pr_archive.claim_pr_archive_checks() == []
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT archived_at FROM agent_threads WHERE id = $1", published["thread_id"]
            )
            is None
        )
        assert (
            await conn.fetchval(
                "SELECT state FROM agent_pr_archive_checks WHERE thread_id = $1",
                published["thread_id"],
            )
            == "pending"
        )


async def test_copying_a_pr_into_fork_history_does_not_associate_the_fork(pool, store):
    source = await _published_job(pool, store)
    inherited = await _published_job(pool, store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET fork_source_job_id = $2 WHERE id = $1",
            inherited["id"],
            source["id"],
        )
    checks = await store.pr_archive.claim_pr_archive_checks()
    assert [check["thread_id"] for check in checks] == [source["thread_id"]]
    assert await _finish(store, checks[0]) is not None
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT archived_at FROM agent_threads WHERE id = $1", inherited["thread_id"]
            )
            is None
        )


async def test_completed_receipt_survives_restore_and_never_rearchives_same_pr(pool, store):
    job = await _published_job(pool, store)
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    receipt = await _finish(store, check)
    assert receipt is not None
    restored = await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=job["user_id"], archived=False
    )
    assert restored is not None and restored["archived_at"] is None
    assert await store.pr_archive.claim_pr_archive_checks() == []
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_pr_archive_checks") == 1
        assert await conn.fetchval("SELECT state FROM agent_pr_archive_checks") == "archived"
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_job_events "
                "WHERE payload->>'phase' = 'session_archived_pr_merged'"
            )
            == 1
        )


async def test_parallel_claims_are_disjoint_and_expired_worker_is_fenced(pool, store):
    jobs = [await _published_job(pool, store) for _ in range(4)]
    for discovered in await store.pr_archive.claim_pr_archive_checks():
        assert await _release(store, discovered)
    batches = await asyncio.gather(
        store.pr_archive.claim_pr_archive_checks(limit=2),
        store.pr_archive.claim_pr_archive_checks(limit=2),
    )
    checks = [check for batch in batches for check in batch]
    assert len(checks) == len({check["thread_id"] for check in checks}) == 4
    assert {check["thread_id"] for check in checks} == {job["thread_id"] for job in jobs}
    assert await store.pr_archive.claim_pr_archive_checks() == []
    stale = checks[0]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pr_archive_checks "
            "SET lease_expires_at = NOW() - interval '1 second' WHERE thread_id = $1",
            stale["thread_id"],
        )
    assert not await _release(store, stale)
    assert await _finish(store, stale) is None
    replacement = (await store.pr_archive.claim_pr_archive_checks())[0]
    assert replacement["thread_id"] == stale["thread_id"]
    assert replacement["claim_token"] != stale["claim_token"]
    assert not await _release(store, stale, terminal=True)
    assert await _finish(store, stale) is None
    assert await _finish(store, replacement) is not None


async def test_retries_persist_schedule_failures_and_terminal_skips(pool, store):
    await _published_job(pool, store)
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    assert await _release(store, check, delay_s=30, error_code="github_rate_limited")
    assert await store.pr_archive.claim_pr_archive_checks() == []
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_pr_archive_checks")
        assert row["failures"] == 1 and row["last_error"] == "github_rate_limited"
        assert row["next_check_at"] > row["updated_at"]
        await conn.execute("UPDATE agent_pr_archive_checks SET next_check_at = NOW()")
    retried = (await store.pr_archive.claim_pr_archive_checks())[0]
    assert retried["failures"] == 1
    assert await _release(store, retried)
    checked = (await store.pr_archive.claim_pr_archive_checks())[0]
    assert checked["failures"] == 0
    assert await _release(store, checked, terminal=True, error_code="invalid_pr_url")
    assert await store.pr_archive.claim_pr_archive_checks() == []
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_pr_archive_checks")
        assert row["state"] == "skipped" and row["completed_at"] is not None
        assert await conn.fetchval("SELECT archived_at FROM agent_threads") is None


async def test_access_recovery_resumes_a_terminal_skipped_check(pool, store):
    """A repaired GitHub installation must resume a skipped archival check.

    Access denial becomes terminal only after the configured failure budget.
    Once the installation is restored, the durable obligation must become
    eligible again without operator SQL; the worker then revalidates owner,
    publication, PR identity, merge state and archival fencing as usual.
    """
    await _published_job(pool, store)
    await _skip_for_access(pool, store)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, failures, completed_at FROM agent_pr_archive_checks"
        )
        assert row["state"] == "skipped" and row["failures"] == 10
        assert row["completed_at"] is not None

    # Simulate the repaired installation: the next poll must reclaim and then
    # verify the still-current published PR before archiving it.
    recovered = (await _recover(store))[0]
    assert await _finish(store, recovered) is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, completed_at, archived_at FROM agent_pr_archive_checks"
        )
        assert row["state"] == "archived"
        assert row["completed_at"] is not None
        assert await conn.fetchval("SELECT archived_at FROM agent_threads") is not None


async def test_access_recovery_stops_once_its_attempts_are_spent(pool, store):
    """A PR or repository that is gone keeps answering 404: stop asking."""
    await _published_job(pool, store)
    await _skip_for_access(pool, store)
    # Only a caller that asks for recovery gets the skipped check.
    assert await store.pr_archive.claim_pr_archive_checks() == []
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pr_archive_checks SET failures = $1", ACCESS_RECOVERY_FAILURES - 1
        )
    last = (await _recover(store))[0]
    await _release(store, last, error_code="github_access_denied", terminal=True)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_pr_archive_checks SET next_check_at = NOW()")
        row = await conn.fetchrow("SELECT state, failures FROM agent_pr_archive_checks")
    assert dict(row) == {"state": "skipped", "failures": ACCESS_RECOVERY_FAILURES}
    assert await _recover(store) == []


async def test_only_an_access_failure_is_recovered(pool, store):
    """A mismatched PR identity is final; a repaired installation cannot fix it."""
    await _published_job(pool, store)
    await _skip_for_access(pool, store, error_code="pr_identity_mismatch")
    assert await _recover(store) == []


async def test_merge_goal_pause_and_receipt_roll_back_if_event_write_fails(
    pool, store, monkeypatch
):
    job = await _published_job(pool, store)
    goal = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="Ship the change", max_rounds=5),
    )
    assert goal is not None
    check = (await store.pr_archive.claim_pr_archive_checks())[0]

    async def fail_event(*args, **kwargs):
        raise RuntimeError("injected event-write failure")

    with monkeypatch.context() as patch:
        patch.setattr(repositories.pr_archive, "insert_event", fail_event)
        with pytest.raises(RuntimeError, match="injected event-write failure"):
            await _finish(store, check)
    async with pool.acquire() as conn:
        thread = await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"])
        assert thread["archived_at"] is None and thread["goal_state"] == "active"
        assert thread["queue_paused_reason"] is None
        receipt = await conn.fetchrow("SELECT * FROM agent_pr_archive_checks")
        assert receipt["state"] == "pending" and receipt["claim_token"] == check["claim_token"]
    assert await _finish(store, check) is not None
    async with pool.acquire() as conn:
        thread = await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"])
        assert thread["goal_state"] == "paused" and thread["goal_pause_reason"] == "archived"
        assert thread["queue_paused_reason"] is None
        assert await conn.fetchval("SELECT state FROM agent_jobs") == "succeeded"


@pytest.mark.parametrize("held", [False, True])
async def test_new_waiting_owner_work_defers_a_previously_claimed_archive(pool, store, held):
    job = await _published_job(pool, store)
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    submitted = await store.pending_messages.submit_message(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        prompt="Continue this work",
        skill_ids=None,
        attachment_ids=None,
        model=None,
        reasoning_effort=None,
    )
    assert "message" in submitted
    message = submitted["message"]
    if held:
        edited = await store.pending_messages.edit_message(
            thread_id=job["thread_id"],
            user_id=job["user_id"],
            message_id=message["id"],
            revision=message["revision"],
            changes={"state": "held"},
        )
        message = edited["message"]
    assert await _finish(store, check) is None
    assert await _release(store, check)
    assert await store.pr_archive.claim_pr_archive_checks() == []
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT archived_at FROM agent_threads") is None
        assert (
            await conn.fetchval("SELECT prompt FROM agent_pending_messages") == "Continue this work"
        )
        assert await conn.fetchval("SELECT queue_paused_reason FROM agent_threads") is None
    # Removing the waiting note explicitly restores eligibility; no accepted
    # owner work is silently held back by an archival action.
    assert "error" not in await store.pending_messages.delete_message(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        message_id=message["id"],
        revision=message["revision"],
    )
    current = (await store.pr_archive.claim_pr_archive_checks())[0]
    assert await _finish(store, current) is not None


@pytest.mark.parametrize("change", ["owner", "claim", "deleted", "publication"])
async def test_changed_identity_or_publication_cannot_archive(pool, store, change):
    job = await _published_job(pool, store)
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    overrides = {}
    if change == "owner":
        overrides["user_id"] = "another-user"
    elif change == "claim":
        overrides["claim_token"] = "another-token"
    else:
        async with pool.acquire() as conn:
            if change == "deleted":
                await conn.execute(
                    "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", job["thread_id"]
                )
            else:
                await conn.execute(
                    "UPDATE agent_jobs SET published_pr_url = $2 WHERE id = $1",
                    job["id"],
                    PR_URL.replace("42", "99"),
                )
    assert await _finish(store, check, **overrides) is None
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT archived_at FROM agent_threads") is None
        assert await conn.fetchval("SELECT state FROM agent_pr_archive_checks") == "pending"


async def test_owner_message_committed_while_archive_waits_for_thread_lock_is_seen(pool, store):
    job = await _published_job(pool, store)
    check = (await store.pr_archive.claim_pr_archive_checks())[0]
    task = None
    try:
        async with pool.acquire() as writer, writer.transaction():
            await writer.execute(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", job["thread_id"]
            )
            task = asyncio.create_task(_finish(store, check))
            async with asyncio.timeout(5), pool.acquire() as observer:
                while not await observer.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query LIKE 'SELECT id FROM agent_threads%')"
                ):
                    await asyncio.sleep(0.01)
            await writer.execute(
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ('pending-lock-race', $1, $2, 'queued', 'Accepted before archival')",
                job["thread_id"],
                job["user_id"],
            )
        assert await task is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT archived_at FROM agent_threads") is None
        assert (
            await conn.fetchval("SELECT prompt FROM agent_pending_messages")
            == "Accepted before archival"
        )
        assert await conn.fetchval("SELECT state FROM agent_pr_archive_checks") == "pending"


async def test_github_poll_archives_real_session_and_restore_is_not_reprocessed(
    pool, store, monkeypatch
):
    job = await _published_job(pool, store, retain=True)
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", job["repo"])
    requests = []

    def github(request):
        requests.append(request)
        assert request.url.path == "/repos/example-org/example-repo/pulls/42"
        return httpx.Response(
            200,
            json={
                "number": 42,
                "html_url": PR_URL,
                "base": {"repo": {"full_name": job["repo"]}},
                "state": "closed",
                "merged": True,
                "merged_at": MERGED_AT.isoformat(),
                "merge_commit_sha": "a" * 40,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        assert (
            await poll_pr_merge_archives(
                store, credential_provider=lambda: GitHubCredential("test-only"), client=client
            )
            == 1
        )
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT archived_at FROM agent_threads") is not None
            receipt = await conn.fetchrow("SELECT * FROM agent_pr_archive_checks")
            assert receipt["state"] == "archived" and receipt["merged_at"] == MERGED_AT
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_job_events "
                    "WHERE payload->>'phase' = 'session_archived_pr_merged'"
                )
                == 1
            )
            sandbox_before = await conn.fetchrow("SELECT * FROM agent_sandboxes")
        assert (
            await store.conversation_commands.set_thread_archived(
                job_id=job["id"], user_id=job["user_id"], archived=False
            )
            is not None
        )
        assert (
            await poll_pr_merge_archives(
                store, credential_provider=lambda: GitHubCredential("test-only"), client=client
            )
            == 0
        )
    assert len(requests) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT archived_at FROM agent_threads") is None
        assert await conn.fetchrow("SELECT * FROM agent_sandboxes") == sandbox_before


async def test_concurrent_recovery_workers_claim_exactly_once(pool, store):
    """Two workers racing a recovered access-denied check get one live claim."""
    await _published_job(pool, store)
    first = await _skip_for_access(pool, store)

    results = await asyncio.gather(_recover(store), _recover(store))
    claimed = [check for batch in results for check in batch]
    assert len(claimed) == 1
    assert claimed[0]["claim_token"] != first["claim_token"]


async def test_stale_lease_after_recovery_cannot_archive_or_reschedule(pool, store):
    """A stale claim token after lease expiry cannot mutate the recovered row."""
    await _published_job(pool, store)
    await _skip_for_access(pool, store)

    recovered = (await _recover(store))[0]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pr_archive_checks SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE thread_id = $1 AND pr_url = $2",
            recovered["thread_id"],
            recovered["pr_url"],
        )
    assert not await _release(store, recovered)
    assert await _finish(store, recovered) is None


async def test_new_turn_after_recovery_blocks_archival(pool, store):
    """A newer unpublished turn prevents a recovered merged PR from archiving."""
    job = await _published_job(pool, store, retain=True)
    await _skip_for_access(pool, store)

    recovered = (await _recover(store))[0]
    newer = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="Start the next change"
    )
    assert newer is not None and "error" not in newer
    assert await _finish(store, recovered) is None
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT archived_at FROM agent_threads WHERE id = $1", job["thread_id"]
            )
            is None
        )
