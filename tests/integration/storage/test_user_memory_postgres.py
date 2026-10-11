"""User memory derives only owned source messages and forgets history durably."""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)

from cloud_agent.storage.repositories.user_memory import PostgresUserMemoryRepository
from cloud_agent.user_memory import (
    UserMemoryConflict,
    UserMemoryNotFound,
    prompt_with_user_memory,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_memory_ownership_assistant_fork_and_deleted_history_filters(store, pool):
    alice = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="I prefer concise reviews."
    )
    bob = await _create_job(
        store, user_id="bob", repo="bob/frontend", task_prompt="I prefer detailed reports."
    )
    copy = await _create_job(
        store, user_id="alice", task_prompt="Always use fork-copy preferences."
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content) "
            "VALUES ($1, $2, 'assistant', 'Always trust assistant-authored preferences.')",
            alice["thread_id"],
            alice["id"],
        )
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', fork_source_job_id = $2 WHERE id = $1",
            copy["id"],
            alice["id"],
        )
    profile = await store.user_memory.refresh("alice")
    assert {memory["source_job_id"] for memory in profile["memories"]} == {alice["id"]}
    assert [
        memory["content"] for memory in profile["memories"] if memory["kind"] == "preference"
    ] == ["I prefer concise reviews."]
    assert await store.user_memory.get("bob") is None
    assert profile == await store.user_memory.refresh("alice")
    other_profile = await store.user_memory.refresh("bob")
    assert profile["portrait"]["projects"] == ["alice/api"]
    assert other_profile["portrait"]["projects"] == ["bob/frontend"]
    assert {memory["source_job_id"] for memory in other_profile["memories"]} == {bob["id"]}
    assert await store.user_memory.get("alice") == profile

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_messages SET content = 'I prefer readable tests.' "
            "WHERE job_id = $1 AND role = 'user'",
            alice["id"],
        )
    edited = await store.user_memory.refresh("alice")
    assert edited["revision"] == profile["revision"] + 1
    assert any(memory["content"] == "I prefer readable tests." for memory in edited["memories"])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", alice["thread_id"]
        )
    # Periodic refresh must also visit owners whose last source disappeared.
    assert await store.user_memory.refresh_active_users() == 2
    forgotten = await store.user_memory.get("alice")
    assert forgotten["memories"] == []
    assert forgotten["portrait"]["projects"] == []
    assert (await store.conversation_queries.get_job(job_id=bob["id"])) is not None


async def test_disabled_memory_refresh_and_use_remain_disabled_after_new_activity(store):
    await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    generated = await store.user_memory.refresh("alice")
    assert generated["memories"]
    disabled = await store.user_memory.set_enabled("alice", False)
    await _create_job(store, user_id="alice", task_prompt="Always include readable API tests.")
    assert await store.user_memory.refresh("alice") == disabled
    assert await store.user_memory.set_enabled("alice", False) == disabled
    assert await store.user_memory.refresh_active_users() == 0
    assert await store.user_memory.get("alice") == disabled
    assert prompt_with_user_memory("Current task", disabled, "example-org/example-repo") == (
        "Current task"
    )


async def test_goal_synthetic_messages_are_excluded_but_human_midturn_is_remembered(store, pool):
    goal = await _create_job(
        store, user_id="alice", task_prompt="Always repeat synthetic goal preferences."
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET turn_source = 'goal' WHERE id = $1", goal["id"])
    assert await store.user_memory.refresh_active_users() == 0
    assert await store.user_memory.get("alice") is None
    synthetic = await store.user_memory.refresh("alice")
    assert synthetic["memories"] == synthetic["suggestions"] == []
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn) "
            "VALUES ($1, $2, 'user', 'I prefer readable human-authored tests.', TRUE)",
            goal["thread_id"],
            goal["id"],
        )
    assert await store.user_memory.refresh_active_users() == 1
    authored = await store.user_memory.get("alice")
    preferences = [memory for memory in authored["memories"] if memory["kind"] == "preference"]
    assert [memory["content"] for memory in preferences] == [
        "I prefer readable human-authored tests."
    ]
    assert {memory["source_job_id"] for memory in preferences} == {goal["id"]}


async def test_memory_clear_while_disabled_fences_hidden_history_and_retains_future_input(
    store, pool
):
    original = await _create_job(store, user_id="alice", task_prompt="Always test old changes.")
    await store.user_memory.refresh("alice")
    await store.user_memory.set_enabled("alice", False)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", original["thread_id"]
        )
    cleared = await store.user_memory.clear("alice")
    assert cleared["enabled"] is False
    assert cleared["memories"] == cleared["suggestions"] == []
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NULL WHERE id = $1", original["thread_id"]
        )
    await store.user_memory.set_enabled("alice", True)
    assert (await store.user_memory.refresh("alice"))["memories"] == []
    future = await _create_job(store, user_id="alice", task_prompt="I prefer readable APIs.")
    new_profile = await store.user_memory.refresh("alice")
    assert {memory["source_job_id"] for memory in new_profile["memories"]} == {future["id"]}


async def test_clear_fences_old_source_committed_after_reset(store, pool):
    """A transaction begun before forgetting cannot repopulate the profile later."""
    job = await _create_job(store, user_id="alice", task_prompt="Build an API.")
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_thread_messages WHERE job_id = $1", job["id"])
    await store.user_memory.refresh("alice")
    async with pool.acquire() as source_conn, source_conn.transaction():
        message_id = await source_conn.fetchval(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content) "
            "VALUES ($1, $2, 'user', 'I prefer forgotten preferences.') RETURNING id",
            job["thread_id"],
            job["id"],
        )
        cleared = await store.user_memory.clear("alice")
        async with pool.acquire() as conn:
            fence = await conn.fetchrow(
                "SELECT cleared_message_id, cleared_at FROM agent_user_memories WHERE user_id = 'alice'"
            )
        assert message_id > fence["cleared_message_id"]
        assert await source_conn.fetchval(
            "SELECT created_at < $2 FROM agent_thread_messages WHERE id = $1",
            message_id,
            fence["cleared_at"],
        )
    assert await store.user_memory.refresh("alice") == cleared
    assert cleared["memories"] == []


@pytest.mark.parametrize("control", ["disable", "clear"])
@pytest.mark.parametrize("first", ["refresh", "control"])
async def test_real_transactions_serialize_refresh_against_privacy_controls(
    store, pool, monkeypatch, control, first
):
    await _create_job(store, user_id="alice", task_prompt="I prefer isolated changes.")
    await store.user_memory.refresh("alice")
    repository = PostgresUserMemoryRepository(pool)
    original = repository._user_memory_lock
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_lock(conn, user_id):
        await original(conn, user_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(repository, "_user_memory_lock", paused_lock)

    async def change(repo):
        return (
            await repo.set_enabled("alice", False)
            if control == "disable"
            else await repo.clear("alice")
        )

    holding = asyncio.create_task(
        repository.refresh("alice") if first == "refresh" else change(repository)
    )
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        waiting = asyncio.create_task(
            change(store.user_memory) if first == "refresh" else store.user_memory.refresh("alice")
        )

        async def wait_for_database_lock():
            while True:
                async with pool.acquire() as conn:
                    blocked = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND NOT l.granted "
                        "AND a.datname = current_database())"
                    )
                if blocked:
                    return
                await asyncio.sleep(0.01)

        # Observe the second live transaction waiting in PostgreSQL itself.
        await asyncio.wait_for(wait_for_database_lock(), timeout=5)
        await asyncio.wait_for(store.user_memory.set_enabled("bob", False), timeout=5)
        assert not waiting.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(holding, waiting), timeout=5)
    finally:
        release.set()
        tasks = [holding, *([waiting] if waiting is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
    result = await store.user_memory.refresh("alice")
    if control == "disable":
        assert result["enabled"] is False
    else:
        assert result["memories"] == result["suggestions"] == []


def _preference(profile):
    return next(memory for memory in profile["memories"] if memory["kind"] == "preference")


async def _change_entry(repository, profile, memory_id, operation):
    if operation == "edit":
        return await repository.update_entry(
            "alice",
            memory_id,
            content="Keep API tests readable.",
            expected_revision=profile["revision"],
        )
    return await repository.delete_entry("alice", memory_id, expected_revision=profile["revision"])


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_entry_changes_are_durable_across_repository_and_background_refresh(
    store, pool, operation
):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    changed = await _change_entry(store.user_memory, original, entry["id"], operation)
    assert changed["revision"] == original["revision"] + 1
    fresh_repository = PostgresUserMemoryRepository(pool)
    assert await fresh_repository.get("alice") == changed
    assert await fresh_repository.refresh("alice") == changed

    await _create_job(store, user_id="alice", task_prompt="Build a backend worker.")
    assert await fresh_repository.refresh_active_users() == 1
    refreshed = await fresh_repository.get("alice")
    matches = [memory for memory in refreshed["memories"] if memory["id"] == entry["id"]]
    if operation == "edit":
        assert len(matches) == 1
        assert matches[0] == {
            **entry,
            "content": "Keep API tests readable.",
            "user_edited": True,
        }
        assert matches[0]["source_job_id"] == source["id"]
        assert "Keep API tests readable." in prompt_with_user_memory(
            "Current task", refreshed, source["repo"]
        )
        assert prompt_with_user_memory("Current task", refreshed, "other/repo") == "Current task"
    else:
        assert matches == []
        assert "I prefer concise reviews." not in prompt_with_user_memory(
            "Current task", refreshed, source["repo"]
        )
    async with pool.acquire() as conn:
        saved = await conn.fetchrow(
            "SELECT content, source_job_id FROM agent_user_memory_edits "
            "WHERE user_id = 'alice' AND memory_id = $1",
            entry["id"],
        )
    assert saved["source_job_id"] == source["id"]
    assert saved["content"] == ("Keep API tests readable." if operation == "edit" else None)


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_paused_entry_changes_preserve_snapshot_and_survive_resume(store, operation):
    await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    paused = await store.user_memory.set_enabled("alice", False)
    newer = await _create_job(store, user_id="alice", task_prompt="Always include backend tests.")
    changed = await _change_entry(store.user_memory, paused, entry["id"], operation)
    assert changed["enabled"] is False
    assert changed["portrait"] == paused["portrait"]
    assert changed["suggestions"] == paused["suggestions"]
    assert all(memory["source_job_id"] != newer["id"] for memory in changed["memories"])
    assert await store.user_memory.refresh("alice") == changed
    assert prompt_with_user_memory("Current task", changed, newer["repo"]) == "Current task"
    resumed = await store.user_memory.set_enabled("alice", True)
    assert resumed["enabled"] is True
    assert any(memory["source_job_id"] == newer["id"] for memory in resumed["memories"])
    matching = [memory for memory in resumed["memories"] if memory["id"] == entry["id"]]
    assert [memory["content"] for memory in matching] == (
        ["Keep API tests readable."] if operation == "edit" else []
    )


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_entry_changes_cannot_address_another_owner_or_missing_entry(store, pool, operation):
    await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="I prefer concise reviews."
    )
    alice = await store.user_memory.refresh("alice")
    entry = _preference(alice)
    await _create_job(store, user_id="bob", repo="bob/api", task_prompt="I prefer readable tests.")
    bob = await store.user_memory.refresh("bob")
    for user_id, profile, memory_id in [
        ("bob", bob, entry["id"]),
        ("alice", alice, "missing-entry"),
    ]:
        with pytest.raises(UserMemoryNotFound):
            if operation == "edit":
                await store.user_memory.update_entry(
                    user_id,
                    memory_id,
                    content="Keep API tests readable.",
                    expected_revision=profile["revision"],
                )
            else:
                await store.user_memory.delete_entry(
                    user_id, memory_id, expected_revision=profile["revision"]
                )
    assert await store.user_memory.get("alice") == alice
    assert await store.user_memory.get("bob") == bob
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0


@pytest.mark.parametrize(
    "content",
    ["", " \n\t", "x" * 501, "password=fixture", "Use this for the current task only."],
)
async def test_invalid_preference_edits_leave_saved_profile_and_overrides_unchanged(
    store, pool, content
):
    await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    with pytest.raises(ValueError):
        await store.user_memory.update_entry(
            "alice",
            _preference(original)["id"],
            content=content,
            expected_revision=original["revision"],
        )
    assert await store.user_memory.get("alice") == original
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0


async def test_task_entry_edit_is_normalized_and_never_injected_as_preference(store):
    job = await _create_job(store, user_id="alice", task_prompt="Build a backend worker.")
    original = await store.user_memory.refresh("alice")
    entry = next(memory for memory in original["memories"] if memory["kind"] == "task")
    changed = await store.user_memory.update_entry(
        "alice",
        entry["id"],
        content="  Always  include tests for the current task. \n",
        expected_revision=original["revision"],
    )
    task = next(memory for memory in changed["memories"] if memory["id"] == entry["id"])
    assert task["content"] == "Always include tests for the current task."
    assert task["kind"] == "task"
    assert task["source_repo"] == job["repo"]
    assert task["source_job_id"] == job["id"]
    assert task["user_edited"] is True
    assert prompt_with_user_memory("Current task", changed, job["repo"]) == "Current task"


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_entry_mutation_refreshes_enabled_sources_before_checking_revision(
    store, pool, operation
):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    await _create_job(store, user_id="alice", task_prompt="Always include backend tests.")
    with pytest.raises(UserMemoryConflict):
        await _change_entry(store.user_memory, original, entry["id"], operation)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0
    current = await store.user_memory.refresh("alice")
    assert current["revision"] > original["revision"]
    assert _preference(original)["source_job_id"] == source["id"]
    changed = await _change_entry(store.user_memory, current, entry["id"], operation)
    assert changed["revision"] == current["revision"] + 1
    with pytest.raises(UserMemoryConflict):
        await _change_entry(store.user_memory, current, entry["id"], operation)


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_clear_removes_entry_overrides_and_fences_prior_sources(store, pool, operation):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    await _change_entry(store.user_memory, original, entry["id"], operation)
    cleared = await store.user_memory.clear("alice")
    assert cleared["memories"] == cleared["suggestions"] == []
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0
    assert await store.user_memory.refresh("alice") == cleared
    assert (await store.conversation_queries.get_job(job_id=source["id"])) is not None
    newer = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    refreshed = await store.user_memory.refresh("alice")
    future_entry = _preference(refreshed)
    assert future_entry["id"] == entry["id"]
    assert future_entry["source_job_id"] == newer["id"]
    assert future_entry["content"] == "I prefer concise reviews."
    assert future_entry["user_edited"] is False


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_deleted_source_hides_content_override_and_retains_removal_tombstone(
    store, pool, operation
):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    await _change_entry(store.user_memory, original, entry["id"], operation)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", source["thread_id"]
        )
    assert await store.user_memory.refresh_active_users() == 1
    assert (await store.user_memory.get("alice"))["memories"] == []
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == (
            0 if operation == "edit" else 1
        )
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NULL WHERE id = $1", source["thread_id"]
        )
    restored = await store.user_memory.refresh("alice")
    matches = [memory for memory in restored["memories"] if memory["id"] == entry["id"]]
    assert [memory["content"] for memory in matches] == (
        ["I prefer concise reviews."] if operation == "edit" else []
    )


async def test_content_edit_provenance_does_not_apply_to_newer_duplicate_hash(store, pool):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    await _change_entry(store.user_memory, original, entry["id"], "edit")
    newer = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    duplicate = _preference(await store.user_memory.refresh("alice"))
    assert duplicate["id"] == entry["id"]
    assert duplicate["source_job_id"] == newer["id"]
    assert duplicate["content"] == "I prefer concise reviews."
    assert duplicate["user_edited"] is False
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 1
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", newer["thread_id"]
        )
    previous = _preference(await store.user_memory.refresh("alice"))
    assert previous["source_job_id"] == source["id"]
    assert previous["content"] == "Keep API tests readable."
    assert previous["user_edited"] is True
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NULL WHERE id = $1", newer["thread_id"]
        )
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", source["thread_id"]
        )
    current = _preference(await store.user_memory.refresh("alice"))
    assert current["source_job_id"] == newer["id"]
    assert current["content"] == "I prefer concise reviews."
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0


async def test_removal_tombstone_suppresses_same_hash_from_a_new_source(store, pool):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    await _change_entry(store.user_memory, original, entry["id"], "delete")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", source["thread_id"]
        )
    await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    refreshed = await store.user_memory.refresh("alice")
    assert all(memory["id"] != entry["id"] for memory in refreshed["memories"])
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 1
        assert await conn.fetchval("SELECT content FROM agent_user_memory_edits") is None


@pytest.mark.parametrize("after_eviction", ["restore", "delete_source"])
async def test_source_window_eviction_hides_edit_and_tracks_live_provenance(
    store, pool, after_eviction
):
    source = await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    await _change_entry(store.user_memory, original, entry["id"], "edit")
    newer = await _create_job(store, user_id="alice", task_prompt="Build a backend worker.")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn, created_at) "
            "SELECT $1, $2, 'user', 'Build backend worker number ' || n, TRUE, "
            "clock_timestamp() + n * INTERVAL '1 second' FROM generate_series(1, 50) n",
            newer["thread_id"],
            newer["id"],
        )
    evicted = await store.user_memory.refresh("alice")
    assert all(memory["id"] != entry["id"] for memory in evicted["memories"])
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 1
        if after_eviction == "restore":
            await conn.execute("DELETE FROM agent_thread_messages WHERE job_id = $1", newer["id"])
        else:
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", source["thread_id"]
            )
    refreshed = await store.user_memory.refresh("alice")
    if after_eviction == "restore":
        restored = _preference(refreshed)
        assert restored["id"] == entry["id"]
        assert restored["content"] == "Keep API tests readable."
        assert restored["user_edited"] is True
    else:
        # Its deleted source was already outside the latest-50 activity digest.
        # Cleanup must still run when no visible input changed.
        assert refreshed == evicted
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM agent_user_memory_edits") == 0


@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_entry_revision_fence_rejects_a_second_live_mutation(
    store, pool, monkeypatch, operation
):
    await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    repository = PostgresUserMemoryRepository(pool)
    original_lock = repository._user_memory_lock
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_lock(conn, user_id):
        await original_lock(conn, user_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(repository, "_user_memory_lock", paused_lock)
    holding = asyncio.create_task(_change_entry(repository, original, entry["id"], operation))
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        competing = "delete" if operation == "edit" else "edit"
        waiting = asyncio.create_task(
            _change_entry(store.user_memory, original, entry["id"], competing)
        )
        release.set()
        changed = await asyncio.wait_for(holding, timeout=5)
        with pytest.raises(UserMemoryConflict):
            await asyncio.wait_for(waiting, timeout=5)
        assert await store.user_memory.get("alice") == changed
        assert changed["revision"] == original["revision"] + 1
    finally:
        release.set()
        tasks = [holding, *([waiting] if waiting is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("operation", ["edit", "delete"])
@pytest.mark.parametrize("control", ["disable", "clear"])
@pytest.mark.parametrize("first", ["mutation", "control"])
async def test_live_entry_transactions_serialize_against_privacy_controls(
    store, pool, monkeypatch, operation, control, first
):
    await _create_job(store, user_id="alice", task_prompt="I prefer concise reviews.")
    original = await store.user_memory.refresh("alice")
    entry = _preference(original)
    repository = PostgresUserMemoryRepository(pool)
    original_lock = repository._user_memory_lock
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_lock(conn, user_id):
        await original_lock(conn, user_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(repository, "_user_memory_lock", paused_lock)

    async def change_control(repo):
        return (
            await repo.set_enabled("alice", False)
            if control == "disable"
            else await repo.clear("alice")
        )

    holding = asyncio.create_task(
        _change_entry(repository, original, entry["id"], operation)
        if first == "mutation"
        else change_control(repository)
    )
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        waiting = asyncio.create_task(
            change_control(store.user_memory)
            if first == "mutation"
            else _change_entry(store.user_memory, original, entry["id"], operation)
        )

        async def wait_for_database_lock():
            while True:
                async with pool.acquire() as conn:
                    blocked = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND NOT l.granted "
                        "AND a.datname = current_database())"
                    )
                if blocked:
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_database_lock(), timeout=5)
        await asyncio.wait_for(store.user_memory.set_enabled("bob", False), timeout=5)
        assert not waiting.done()
        release.set()
        await asyncio.wait_for(holding, timeout=5)
        if first == "control":
            with pytest.raises(UserMemoryConflict):
                await asyncio.wait_for(waiting, timeout=5)
        else:
            await asyncio.wait_for(waiting, timeout=5)
    finally:
        release.set()
        tasks = [holding, *([waiting] if waiting is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
    final = await store.user_memory.get("alice")
    async with pool.acquire() as conn:
        override_count = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_user_memory_edits WHERE user_id = 'alice'"
        )
    if control == "clear":
        assert final["memories"] == final["suggestions"] == []
        assert override_count == 0
    else:
        assert final["enabled"] is False
        assert override_count == (1 if first == "mutation" else 0)
        matches = [memory for memory in final["memories"] if memory["id"] == entry["id"]]
        if first == "control":
            assert matches == [entry]
        elif operation == "delete":
            assert matches == []
        else:
            assert [memory["content"] for memory in matches] == ["Keep API tests readable."]
