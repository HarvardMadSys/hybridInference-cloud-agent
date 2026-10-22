"""PostgreSQL session deletion, titles, and queued-turn lifecycle behavior."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.integration.storage.test_agent_job_store import (  # noqa: F401 - pytest fixtures
    UTC,
    Storage,
    _claim,
    _create_job,
    _reserve_shared_chat,
    _settled,
    append_follow_up,
    asyncio,
    datetime,
    pool,
    pytest,
    store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_a_starting_turn_cannot_revive_a_deleted_chats_residency(pool, store: Storage):
    """Deletion that lands while a worker starts leaves it nothing to run under."""
    job, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-deleted-while-starting", context_status="new"
    )
    first = sandbox["residency_generation"]

    deleted = await store.conversation_commands.delete_sessions(
        thread_ids=[job["thread_id"]], user_id=job["user_id"]
    )
    assert [row["generation"] for row in deleted["released_residencies"]] == [first]

    # The same, still-live attempt asks for a fresh residency to run the turn.
    # A new generation would be one the delete never released or closed.
    assert (
        await store.residency.establish_residency(
            thread_id=job["thread_id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            runtime_id="claude-code",
            runtime_version=sandbox["runtime_version"],
            ttl_seconds=1800,
        )
        is None
    )
    # Nor can it deliver the prompt under the generation the delete released.
    assert not await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=first, job_id=job["id"]
    )
    assert (
        await store.residency.heartbeat_residency(
            thread_id=job["thread_id"], generation=first, host="shared-runner", ttl_seconds=120
        )
        is None
    )
    async with pool.acquire() as conn:
        member = await conn.fetchrow(
            "SELECT residency_generation, residency_status, admitted_job_id "
            "FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            job["thread_id"],
        )
    assert dict(member) == {
        "residency_generation": first,
        "residency_status": "none",
        "admitted_job_id": None,
    }


@pytest.mark.parametrize("archive_available", [False, True])
async def test_sandbox_of_deleted_sessions_is_stopped_then_deleted(
    pool, store: Storage, archive_available: bool
):
    """The last deleted chat takes its sandbox with it; the records stay."""
    from cloud_agent.workers.sandbox_deletion import retire_deleted_session_sandboxes

    source, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": source["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-deleted-chats", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=source["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=source["id"],
        context_status="live",
        native_session_id="native-source",
        idle_timeout_seconds=1800,
        base_sha=source["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=source["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    sibling = await _create_job(store, task_prompt="forked sibling")
    async with pool.acquire() as conn:
        # The settled turn's publication finished, as it would before a
        # sandbox can stop.
        await conn.execute("UPDATE agent_sandbox_snapshots SET state = 'published'")
        await conn.execute(
            "UPDATE agent_sandboxes SET snapshot_state = 'quiescent' WHERE id = $1",
            sandbox["sandbox_id"],
        )
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships (
                thread_id, user_id, sandbox_id, native_session_id, native_state,
                context_status, resident_state
            ) VALUES ($1, 'user-1', $2, 'native-sibling', 'ready', 'live', 'stopped')
            """,
            sibling["thread_id"],
            sandbox["sandbox_id"],
        )

    async def sweep() -> int:
        return await retire_deleted_session_sandboxes(
            store, archive_available=lambda: archive_available
        )

    async def group() -> dict:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, stop_requested_at, deletion_state, deletion_discard "
                "FROM agent_sandboxes WHERE id = $1",
                sandbox["sandbox_id"],
            )
        assert row is not None
        return dict(row)

    # One of its two chats remains: the sandbox is still in use.
    await store.conversation_commands.delete_sessions(
        thread_ids=[source["thread_id"]], user_id="user-1"
    )
    assert await sweep() == 0
    assert (await group())["state"] == "running"

    # The last chat goes. The running sandbox is asked to stop, and the host's
    # next heartbeat receives that stop; nothing is deleted yet.
    await store.conversation_commands.delete_sessions(
        thread_ids=[sibling["thread_id"]], user_id="user-1"
    )
    assert await sweep() == 1
    stopping = await group()
    assert stopping["state"] == "stopping" and stopping["stop_requested_at"] is not None
    assert stopping["deletion_state"] == "none"
    due = await store.sandbox.due_thread_sandbox_stop(
        thread_id=source["thread_id"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        container_id="container-deleted-chats",
        sandbox_generation=sandbox["generation"],
    )
    assert due is not None and due["reason"] == "idle"

    # Once Docker reports it stopped, deletion is queued through the
    # administrator's path: archived first when a bucket exists.
    assert (
        await store.sandbox.reconcile_thread_sandbox_observation(
            thread_id=source["thread_id"],
            host="shared-runner",
            sandbox_id=sandbox["sandbox_id"],
            container_id="container-deleted-chats",
            sandbox_generation=sandbox["generation"],
            running=False,
            activity_active=False,
            idle_remaining_seconds=None,
            members=[],
        )
        == "stopped"
    )
    assert await sweep() == 1
    deleting = await group()
    assert deleting["deletion_state"] == "pending"
    assert deleting["deletion_discard"] is (not archive_available)
    assert await sweep() == 0

    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, source, actor_id FROM agent_sandbox_events "
            "WHERE sandbox_id = $1 AND event_type IN "
            "('deleted_sessions_stop_requested', 'admin_delete_requested') ORDER BY id",
            sandbox["sandbox_id"],
        )
    assert [tuple(row) for row in events] == [
        ("deleted_sessions_stop_requested", "system", "user-deletion-sweeper"),
        ("admin_delete_requested", "system", "user-deletion-sweeper"),
    ]
    # Hidden from the owner, kept in the database.
    retained = await store.conversation_queries.get_job(source["id"])
    assert retained is not None and retained["task_prompt"] == source["task_prompt"]


async def test_generated_title_is_canonical_and_stable_across_turns(pool, store: Storage):
    """The prompt remains intact while each turn reads the same short summary."""
    prompt = "Please implement the CSV export for SPRUCE_8462 with detailed validation."
    job, claim, sandbox = await _reserve_shared_chat(store, task_prompt=prompt)
    assert len(job["title"]) <= 36
    assert job["title_source"] == "fallback"
    assert claim is not None and claim["title_generation_pending"]
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await store.conversation_commands.set_generated_thread_title(
        **fence, title="SPRUCE_8462 CSV export"
    )
    assert not await store.conversation_commands.set_generated_thread_title(
        **fence, title="Second summary"
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None
    assert saved["title"] == "SPRUCE_8462 CSV export"
    assert saved["title_source"] == "generated"
    assert saved["task_prompt"] == prompt
    sandbox_fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **sandbox_fence, container_id="title-container", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **sandbox_fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **sandbox_fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="title-session",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )
    child = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="Now add JSON too"
    )
    assert child is not None and child.get("error") is None
    assert child["title"] == saved["title"]
    assert child["title_source"] == "generated"
    followup_claim = await _claim(
        store,
        worker_id="title-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
    )
    assert followup_claim is not None and not followup_claim["title_generation_pending"]
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=child["id"],
        attempt_id=followup_claim["attempt_id"],
        lease_generation=followup_claim["lease_generation"],
        title="JSON export",
    )


async def test_manual_same_title_wins_in_flight_generation(pool, store: Storage):
    """A manual save claims ownership even if the user keeps the fallback text."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None and claim["title_generation_pending"]
    # Hold the user edit uncommitted so generation really races the row lock.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agent_threads SET title_source = 'manual' WHERE id = $1",
            job["thread_id"],
        )
        generation = asyncio.create_task(
            store.conversation_commands.set_generated_thread_title(
                job_id=job["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                title="Unwanted automatic title",
            )
        )
        await asyncio.sleep(0.03)
        assert not generation.done()
    assert not await asyncio.wait_for(generation, timeout=3)
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == job["title"]
    assert saved["title_source"] == "manual"


async def test_manual_same_title_sets_provenance(store: Storage):
    """Saving the visible fallback without edits still blocks later generation."""
    job = await _create_job(store)
    assert await store.conversation_commands.set_thread_title(
        job_id=job["id"], user_id=job["user_id"], title=job["title"]
    )
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None and not claim["title_generation_pending"]
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title_source"] == "manual"
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        title="Unwanted title",
    )


@pytest.mark.parametrize(
    "fence_break",
    ["generation", "job", "expired", "failed", "cancelled", "cancel_requested", "deleted"],
)
async def test_generated_title_rejects_stale_or_deleted_work(
    pool, store: Storage, fence_break: str
):
    """Expired/superseded work cannot mutate the canonical conversation title."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None
    generation = claim["lease_generation"]
    job_id = job["id"]
    if fence_break == "generation":
        generation += 1
    elif fence_break == "job":
        job_id = (await _create_job(store, user_id="other-owner"))["id"]
    elif fence_break in {"failed", "cancelled"}:
        assert await store.attempt_commands.transition(
            job_id=job_id,
            attempt_id=claim["attempt_id"],
            lease_generation=generation,
            from_states=("running",),
            to_state=fence_break,
        )
    else:
        async with pool.acquire() as conn:
            if fence_break == "expired":
                await conn.execute(
                    "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE id = $1",
                    claim["attempt_id"],
                )
            elif fence_break == "cancel_requested":
                await conn.execute(
                    "UPDATE agent_jobs SET cancel_requested = TRUE WHERE id = $1", job_id
                )
            else:
                await conn.execute(
                    "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1",
                    job["thread_id"],
                )
    assert (
        await store.conversation_commands.get_thread_title_prompt(
            job_id=job_id, attempt_id=claim["attempt_id"], lease_generation=generation
        )
        is None
    )
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=job_id,
        attempt_id=claim["attempt_id"],
        lease_generation=generation,
        title="Stale result",
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == job["title"]


async def test_title_generation_recovers_on_retry_but_rejects_the_old_attempt(store: Storage):
    """A fresh lease can recover naming; the superseded attempt still cannot write."""
    job = await _create_job(store)
    first = await _claim(store, worker_id="first-worker", lease_ttl_seconds=60)
    assert first is not None and first["title_generation_pending"]
    assert await store.attempt_commands.release_claim(
        job_id=job["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
    )
    second = await _claim(store, worker_id="second-worker", lease_ttl_seconds=60)
    assert second is not None and second["title_generation_pending"]
    assert (
        await store.conversation_commands.get_thread_title_prompt(
            job_id=job["id"],
            attempt_id=first["attempt_id"],
            lease_generation=first["lease_generation"],
        )
        is None
    )
    assert not await store.conversation_commands.set_generated_thread_title(
        job_id=job["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        title="Stale summary",
    )
    assert (
        await store.conversation_commands.get_thread_title_prompt(
            job_id=job["id"],
            attempt_id=second["attempt_id"],
            lease_generation=second["lease_generation"],
        )
        == job["task_prompt"]
    )
    assert await store.conversation_commands.set_generated_thread_title(
        job_id=job["id"],
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        title="Recovered summary",
    )


@pytest.mark.parametrize("source", ["fallback", "generated", "inherited", "manual"])
async def test_followup_recovers_original_topic_and_legacy_warmup_title(pool, store, source):
    """A later turn repairs automatic warmup titles without taking manual ownership."""
    prompt = "Implement CSV export for SPRUCE_8462"
    job, claim, sandbox = await _reserve_shared_chat(store, task_prompt=prompt)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert not await store.conversation_commands.set_generated_thread_title(
        **fence, title="⏳ The model is starting up — this t…"
    )
    if source != "fallback":
        # Model the already-persisted bug, its fork, or a deliberate user title.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_threads SET title = $2, title_source = $3 WHERE id = $1",
                job["thread_id"],
                "⏳ The model is starting up — this t…",
                source,
            )
    sandbox_fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **sandbox_fence, container_id="title-recovery", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **sandbox_fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **sandbox_fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="title-recovery-session",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )
    child = await append_follow_up(
        pool, parent_job_id=job["id"], user_id=job["user_id"], prompt="Now add JSON too"
    )
    assert child is not None and child.get("error") is None
    next_claim = await _claim(
        store,
        worker_id="title-recovery-worker",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
    )
    assert next_claim is not None
    assert next_claim["title_generation_pending"] is (source != "manual")
    next_fence = {
        "job_id": child["id"],
        "attempt_id": next_claim["attempt_id"],
        "lease_generation": next_claim["lease_generation"],
    }
    assert await store.conversation_commands.get_thread_title_prompt(**next_fence) == (
        prompt if source != "manual" else None
    )
    assert await store.conversation_commands.set_generated_thread_title(
        **next_fence, title="SPRUCE_8462 CSV export"
    ) is (source != "manual")
    saved = await store.conversation_queries.get_job(child["id"])
    assert saved["title"] == (
        "SPRUCE_8462 CSV export" if source != "manual" else "⏳ The model is starting up — this t…"
    )


async def test_successful_short_task_accepts_delayed_title(pool, store: Storage):
    """A fast task can finish before its independent title request returns."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="title-worker", lease_ttl_seconds=60)
    assert claim is not None
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await _settled(pool, store, fence["job_id"])
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )
    assert await store.conversation_commands.get_thread_title_prompt(**fence) == job["task_prompt"]
    assert await store.conversation_commands.set_generated_thread_title(
        **fence, title="Fix flaky test"
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert saved is not None and saved["title"] == "Fix flaky test"
    assert saved["title_source"] == "generated"


async def test_a_cancelled_parent_released_by_the_platform_unblocks_its_queued_child(
    pool,
    store: Storage,
):
    """A platform-side release that lands as ``cancelled`` is a turn boundary too.

    The child is already queued and becomes eligible as soon as the parent's
    terminal write commits, without waiting for a periodic sweep.
    """
    parent = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == parent["id"]
    child = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="and then this"
    )
    assert child is not None and child["state"] == "queued"

    assert (
        await store.attempt_commands.request_cancel(job_id=parent["id"], user_id="user-1")
        == "running"
    )
    assert await store.attempt_commands.release_claim(
        job_id=parent["id"], attempt_id=claim["attempt_id"]
    )

    assert (await store.conversation_queries.get_job(parent["id"]))["state"] == "cancelled"
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    # And it is the next thing a runner is handed.
    next_claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert next_claim is not None and next_claim["id"] == child["id"]


async def test_queued_follow_up_needs_no_promotion_after_parent_finishes(
    pool,
    store: Storage,
):
    """Eligibility follows the parent state even if no terminal cleanup ran."""
    parent = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    child = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="and then this"
    )
    assert child is not None and child["state"] == "queued"
    # A terminal parent without the scoped continuity cleanup, as can exist
    # in legacy data. Claiming does not depend on a second state update.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', current_attempt_id = NULL WHERE id = $1",
            parent["id"],
        )
        await conn.execute(
            "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() WHERE id = $1",
            claim["attempt_id"],
        )
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    next_claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert next_claim is not None and next_claim["id"] == child["id"]


async def test_queued_cleanup_fails_tail_after_success_without_retained_context(
    pool,
    store: Storage,
):
    """Legacy success cannot cause a queued follow-up to start a fresh sandbox."""
    parent = await _create_job(store)
    child = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="second turn"
    )
    grandchild = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="third turn"
    )
    assert child is not None and grandchild is not None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            parent["id"],
        )
    other = await _create_job(store, task_prompt="independent runnable turn")
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == other["id"]
    assert await _claim(store, worker_id="w2", lease_ttl_seconds=60) is None
    await store.attempt_commands.sweep_queued_follow_ups()
    for pending in (child, grandchild):
        saved = await store.conversation_queries.get_job(pending["id"])
        assert saved["state"] == "failed"
        assert "predates retained-session continuity" in saved["detail"]


@pytest.mark.parametrize("lost_field", ["context_status", "native_state"])
async def test_queued_child_with_lost_membership_is_not_claimed_before_cleanup(
    pool,
    store: Storage,
    lost_field: str,
):
    """A running group does not prove its member's native context is usable."""
    parent, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": parent["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence,
        container_id="container-lost-member",
        context_status="new",
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=parent["id"]
    )
    child = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="second turn"
    )
    assert child is not None and child["state"] == "queued"
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=parent["id"],
        context_status="live",
        native_session_id="native-lost-member",
        idle_timeout_seconds=1800,
        base_sha=parent["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    # Model legacy membership loss without the same-transaction tail cleanup.
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE agent_thread_sandbox_memberships SET {lost_field} = 'lost' "
            "WHERE thread_id = $1",
            parent["thread_id"],
        )
    assert (
        await _claim(
            store,
            worker_id="w2",
            lease_ttl_seconds=60,
            host="shared-runner",
            host_credential_hash="test-credential-hash",
        )
        is None
    )
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "queued"
    await store.attempt_commands.sweep_queued_follow_ups()
    assert (await store.conversation_queries.get_job(child["id"]))["state"] == "failed"


async def test_a_success_that_skipped_settlement_is_refused_loudly(pool, store: Storage):
    """The store does not guess a native ordinal; a protocol slip fails, visibly."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    with pytest.raises(ValueError, match="settle"):
        await store.attempt_commands.transition(
            **fence, from_states=("running",), to_state="succeeded"
        )
    assert (await store.conversation_queries.get_job(job["id"]))["state"] == "running"
    await _settled(pool, store, job["id"])
    assert await store.attempt_commands.transition(
        **fence, from_states=("running",), to_state="succeeded"
    )


async def test_conversations_activity_counts_an_earlier_turn_finishing_last(
    pool,
    store: Storage,
):
    """A cancelled follow-up does not hide the parent's later completion.

    The newest *turn* is the cancelled child; the newest *activity* is the
    parent succeeding after it. The list must order by the latter.
    """
    parent = await _create_job(store, task_prompt="first conversation")
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == parent["id"]
    child = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="never mind"
    )
    assert child is not None and child["state"] == "queued"
    assert (
        await store.attempt_commands.request_cancel(job_id=child["id"], user_id="user-1")
        == "cancelled"
    )
    other = await _create_job(store, task_prompt="second conversation")
    before = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in before["conversations"]] == [other["id"], child["id"]]

    await _settled(pool, store, parent["id"])
    assert await store.attempt_commands.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    after = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in after["conversations"]] == [child["id"], other["id"]]
    finished = await store.conversation_queries.get_job(parent["id"])
    assert after["conversations"][0]["last_activity_at"] == finished["updated_at"]


async def test_conversations_activity_counts_a_publication_of_an_earlier_turn(
    pool,
    store: Storage,
):
    """Publication stamps the turns it included, and that is activity too."""
    parent = await _create_job(store, task_prompt="first conversation")
    child = await append_follow_up(
        pool, parent_job_id=parent["id"], user_id="user-1", prompt="never mind"
    )
    assert child is not None
    assert (
        await store.attempt_commands.request_cancel(job_id=child["id"], user_id="user-1")
        == "cancelled"
    )
    other = await _create_job(store, task_prompt="second conversation")
    before = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in before["conversations"]] == [other["id"], child["id"]]
    # What ``record_snapshot_publish`` writes on every included, succeeded
    # turn: the pull request, the commit, and a fresh ``updated_at``.
    published_at = datetime(2030, 1, 1, tzinfo=UTC)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1, "
            "published_pr_url = 'https://example.test/pr/1', updated_at = $2 WHERE id = $1",
            parent["id"],
            published_at,
        )

    after = await store.conversation_queries.list_conversations(user_id="user-1")
    assert [item["job"]["id"] for item in after["conversations"]] == [child["id"], other["id"]]
    assert after["conversations"][0]["last_activity_at"] == published_at


async def test_side_chat_follow_up_http_receipt_matches_retained_owner_read(
    pool, store, monkeypatch
):
    """Pin the browser's receipt contract through the real SQL command and HTTP route.

    The turn a message becomes is written by SQL that returns JOB_SELECT
    without membership columns. Serializing that row directly used to commit a
    turn but leave every side-chat send looking like a rejected workspace
    identity on the client; the receipt has to be the owner read.
    """
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from cloud_agent.api import routes
    from cloud_agent.api._owner_auth import require_agent_owner
    from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client, get_storage
    from cloud_agent.application import follow_ups

    job, claim, sandbox = await _reserve_shared_chat(store)
    origin = await _create_job(store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET fork_anchor_job_id = $1, forked_from_thread_id = $3 WHERE thread_id = $2",
            origin["id"],
            job["thread_id"],
            origin["thread_id"],
        )
    # The side chat's turn has ended, so a message becomes its next turn in the
    # same request rather than waiting in the queue.
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-side-receipt", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-side-receipt",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    command_rows = []
    settle_queue = store.pending_messages.settle_queue

    async def capture_command_row(**kwargs):
        outcome = await settle_queue(**kwargs)
        if outcome.job is not None:
            command_rows.append(outcome.job)
        return outcome

    monkeypatch.setattr(store.pending_messages, "settle_queue", capture_command_row)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": job["user_id"]}
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_gateway_client] = lambda: None
    monkeypatch.setattr(follow_ups, "require_entitled_repo", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/v1/agent/threads/{job['thread_id']}/messages"
        headers = {"Idempotency-Key": "side-receipt-contract"}
        response = await client.post(path, json={"prompt": "side question"}, headers=headers)
        assert response.status_code == 201, response.text
        assert response.json()["pending"] is None
        receipt = response.json()["job"]
        assert len(command_rows) == 1
        assert command_rows[0]["thread_sandbox"] is None
        replay = await client.post(path, json={"prompt": "side question"}, headers=headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["job"]["id"] == receipt["id"]
        for body in (receipt, replay.json()["job"]):
            assert body["sandbox_id"] == sandbox["sandbox_id"]
            assert body["fork_anchor_job_id"] == origin["id"]
            assert body["thread_id"] == job["thread_id"]
            assert body["workspace_shared"] is True
        # This fixture is consumed by SideChat.test.tsx as well. Normalize only
        # generated IDs, preserving the real HTTP membership projection.
        import json
        from pathlib import Path

        contract = json.loads(
            (Path(__file__).parents[2] / "fixtures/side_chat_job_receipt.json").read_text()
        )
        normalized_ids = {
            sandbox["sandbox_id"]: "shared",
            job["thread_id"]: "side",
            origin["id"]: "main-2",
        }
        assert {key: normalized_ids.get(receipt[key], receipt[key]) for key in contract} == contract
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_jobs WHERE thread_id = $1", job["thread_id"]
                )
                == 2
            )

    await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=job["user_id"], archived=True
    )
    refused = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"],
        user_id=job["user_id"],
        idempotency_key="archived-side",
    )
    assert refused == {"error": "session_archived"}
