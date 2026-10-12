"""Owner and repository scoping, independent controls, and project reset fences."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)

from cloud_agent.storage.repositories.user_memory import PostgresUserMemoryRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_projects_use_their_own_last_50_messages_and_never_share_owner_context(store):
    older = await _create_job(
        store, user_id="alice", repo="shared/api", task_prompt="I prefer readable API tests."
    )
    other = await _create_job(
        store, user_id="bob", repo="shared/api", task_prompt="I prefer detailed API reviews."
    )
    for index in range(51):
        await _create_job(
            store,
            user_id="alice",
            repo="alice/busy",
            task_prompt=f"I prefer busy-project change {index}.",
        )
    alice = await store.user_memory.refresh_project("alice", "shared/api")
    assert {memory["source_job_id"] for memory in alice["memories"]} == {older["id"]}
    assert {memory["scope"] for memory in alice["memories"]} == {"repo"}
    assert alice["portrait"]["projects"] == ["shared/api"]
    assert await store.user_memory.get_project("bob", "shared/api") is None
    bob = await store.user_memory.refresh_project("bob", "shared/api")
    assert {memory["source_job_id"] for memory in bob["memories"]} == {other["id"]}
    assert await store.user_memory.list_memory_projects("alice") == ["alice/busy", "shared/api"]
    assert await store.user_memory.list_memory_projects("bob") == ["shared/api"]
    assert alice == await store.user_memory.refresh_project("alice", "shared/api")


async def test_projects_exclude_assistant_fork_and_goal_messages_but_include_human_midturn(
    store, pool
):
    authored = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="I prefer readable API tests."
    )
    fork = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="Always repeat fork-copy preferences."
    )
    goal = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="Always repeat goal preferences."
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content) "
            "VALUES ($1, $2, 'assistant', 'Always repeat assistant preferences.')",
            authored["thread_id"],
            authored["id"],
        )
        await conn.execute(
            "UPDATE agent_jobs SET state = 'failed', fork_source_job_id = $2 WHERE id = $1",
            fork["id"],
            authored["id"],
        )
        await conn.execute("UPDATE agent_jobs SET turn_source = 'goal' WHERE id = $1", goal["id"])
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn) "
            "VALUES ($1, $2, 'user', 'I prefer human-authored test cases.', TRUE)",
            goal["thread_id"],
            goal["id"],
        )
    project = await store.user_memory.refresh_project("alice", "alice/api")
    assert {
        memory["content"] for memory in project["memories"] if memory["kind"] == "preference"
    } == {"I prefer readable API tests.", "I prefer human-authored test cases."}


async def test_independent_project_controls_and_background_cleanup_after_source_deletion(
    store, pool
):
    api = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="I prefer API tests."
    )
    await _create_job(store, user_id="alice", repo="alice/ui", task_prompt="I prefer UI tests.")
    await store.user_memory.set_enabled("alice", False)
    disabled = await store.user_memory.set_project_enabled("alice", "alice/api", False)
    assert await store.user_memory.refresh_active_users() == 1
    assert (await store.user_memory.get_project("alice", "alice/ui"))["memories"]
    assert await store.user_memory.get_project("alice", "alice/api") == disabled
    await store.user_memory.set_project_enabled("alice", "alice/api", True)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", api["thread_id"]
        )
    await store.user_memory.refresh_active_users()
    cleaned = await store.user_memory.get_project("alice", "alice/api")
    assert cleaned["memories"] == cleaned["suggestions"] == []
    assert cleaned["portrait"]["projects"] == []
    assert await store.user_memory.list_memory_projects("alice") == ["alice/api", "alice/ui"]
    await store.user_memory.set_project_enabled("alice", "alice/api", False)
    await store.user_memory.set_project_enabled("alice", "alice/ui", False)
    assert await store.user_memory.refresh_active_users() == 0


async def test_project_clear_fences_hidden_and_late_committed_sources_and_keeps_other_scope(
    store, pool
):
    original = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="Always test old API changes."
    )
    await _create_job(store, user_id="alice", repo="alice/ui", task_prompt="I prefer UI tests.")
    other = await store.user_memory.refresh_project("alice", "alice/ui")
    await store.user_memory.refresh_project("alice", "alice/api")
    await store.user_memory.set_project_enabled("alice", "alice/api", False)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", original["thread_id"]
        )
    async with pool.acquire() as source_conn, source_conn.transaction():
        message_id = await source_conn.fetchval(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn) "
            "VALUES ($1, $2, 'user', 'I prefer forgotten API preferences.', TRUE) RETURNING id",
            original["thread_id"],
            original["id"],
        )
        cleared = await store.user_memory.clear_project("alice", "alice/api")
        async with pool.acquire() as conn:
            fence = await conn.fetchrow(
                "SELECT cleared_message_id, cleared_at FROM agent_project_memories "
                "WHERE user_id = 'alice' AND repo = 'alice/api'"
            )
        assert message_id > fence["cleared_message_id"]
        assert await source_conn.fetchval(
            "SELECT created_at < $2 FROM agent_thread_messages WHERE id = $1",
            message_id,
            fence["cleared_at"],
        )
    assert cleared["enabled"] is False
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NULL WHERE id = $1", original["thread_id"]
        )
    assert (await store.user_memory.set_project_enabled("alice", "alice/api", True))[
        "memories"
    ] == []
    assert await store.user_memory.get_project("alice", "alice/ui") == other
    future = await _create_job(
        store, user_id="alice", repo="alice/api", task_prompt="I prefer readable new API tests."
    )
    refreshed = await store.user_memory.refresh_project("alice", "alice/api")
    assert {memory["source_job_id"] for memory in refreshed["memories"]} == {future["id"]}


@pytest.mark.parametrize("control", ["disable", "clear"])
async def test_project_refresh_and_privacy_controls_share_real_database_owner_lock(
    store, pool, monkeypatch, control
):
    await _create_job(store, user_id="alice", repo="alice/api", task_prompt="I prefer API tests.")
    repository = PostgresUserMemoryRepository(pool)
    original_lock = repository._user_memory_lock
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause_lock(conn, user_id):
        await original_lock(conn, user_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(repository, "_user_memory_lock", pause_lock)
    holding = asyncio.create_task(repository.refresh_project("alice", "alice/api"))
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        waiting = asyncio.create_task(
            store.user_memory.set_project_enabled("alice", "alice/api", False)
            if control == "disable"
            else store.user_memory.clear_project("alice", "alice/api")
        )

        async def observe_database_wait():
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

        await asyncio.wait_for(observe_database_wait(), timeout=5)
        await asyncio.wait_for(store.user_memory.set_enabled("bob", False), timeout=5)
        assert not waiting.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(holding, waiting), timeout=5)
    finally:
        release.set()
        await asyncio.gather(holding, *([waiting] if waiting else []), return_exceptions=True)
    result = await store.user_memory.refresh_project("alice", "alice/api")
    if control == "disable":
        assert result["enabled"] is False
    else:
        assert result["memories"] == result["suggestions"] == []


async def test_background_cursor_fairly_visits_disabled_users_with_enabled_projects(store, pool):
    owners = ["alice", "bob", "carol", "dana"]
    for owner in owners:
        await _create_job(
            store, user_id=owner, repo=f"{owner}/api", task_prompt="I prefer API tests."
        )
        await store.user_memory.set_enabled(owner, False)
        await store.user_memory.refresh_project(owner, f"{owner}/api")
    # Delete all visible sources so only saved project rows keep these owners eligible.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_threads SET deleted_at = NOW()")
    repository = PostgresUserMemoryRepository(pool)
    for _ in range(2):
        assert await repository.refresh_active_users(limit=2) == 2
    assert repository._active_memory_cursor == "dana"
    for owner in owners:
        assert (await repository.get_project(owner, f"{owner}/api"))["memories"] == []


@pytest.mark.parametrize("source_scope", ["user", "repo"])
async def test_explicit_scope_transfer_is_atomic_and_survives_independent_clear(
    store, pool, source_scope
):
    content = (
        "I prefer concise reviews across all projects."
        if source_scope == "user"
        else "I prefer concise reviews."
    )
    await _create_job(store, user_id="alice", repo="alice/api", task_prompt=content)
    repository = store.user_memory
    source = (
        await repository.refresh("alice")
        if source_scope == "user"
        else await repository.refresh_project("alice", "alice/api")
    )
    entry = next(memory for memory in source["memories"] if memory["kind"] == "preference")
    target_scope = "repo" if source_scope == "user" else "user"
    moved = {**entry, "content": "Keep reviews concise.", "scope": target_scope, "edited": True}
    saved = (
        await repository.replace_memories("alice", source["revision"], [moved])
        if source_scope == "user"
        else await repository.replace_project_memories(
            "alice", "alice/api", source["revision"], [moved]
        )
    )
    assert saved["memories"] == []
    fresh = PostgresUserMemoryRepository(pool)
    await _create_job(store, user_id="alice", repo="alice/api", task_prompt="Add API tests.")
    user = await fresh.refresh("alice")
    project = await fresh.refresh_project("alice", "alice/api")
    assert moved in (user if target_scope == "user" else project)["memories"]
    assert moved not in (project if target_scope == "user" else user)["memories"]
    if source_scope == "user":
        await fresh.clear("alice")
        assert moved in (await fresh.refresh_project("alice", "alice/api"))["memories"]
    else:
        await fresh.clear_project("alice", "alice/api")
        assert moved in (await fresh.refresh("alice"))["memories"]


@pytest.mark.parametrize("source_scope", ["user", "repo"])
@pytest.mark.parametrize("native_guard", [False, True])
async def test_destination_clear_erases_hidden_transfer_text_but_keeps_source_suppression(
    store, pool, source_scope, native_guard
):
    content = (
        "I prefer concise reviews across all projects."
        if source_scope == "user"
        else "I prefer concise reviews."
    )
    await _create_job(store, user_id="alice", repo="alice/api", task_prompt=content)
    repository = store.user_memory
    source = (
        await repository.refresh("alice")
        if source_scope == "user"
        else await repository.refresh_project("alice", "alice/api")
    )
    entry = next(memory for memory in source["memories"] if memory["kind"] == "preference")
    moved = {
        **entry,
        "content": "Keep transferred reviews concise.",
        "scope": "repo" if source_scope == "user" else "user",
        "edited": True,
    }
    if source_scope == "user":
        await repository.replace_memories("alice", source["revision"], [moved])
    else:
        await repository.replace_project_memories("alice", "alice/api", source["revision"], [moved])
    if native_guard:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_user_memories SET overrides = "
                "jsonb_set(overrides, '{0,native_source_job_id}', to_jsonb($1::text)) "
                "WHERE user_id = 'alice'"
                if source_scope == "user"
                else "UPDATE agent_project_memories SET overrides = "
                "jsonb_set(overrides, '{0,native_source_job_id}', to_jsonb($1::text)) "
                "WHERE user_id = 'alice' AND repo = 'alice/api'",
                moved["source_job_id"],
            )
    unrelated = (
        "I prefer readable tests across all projects."
        if source_scope == "user"
        else "I prefer readable tests."
    )
    await _create_job(store, user_id="alice", repo="alice/api", task_prompt=unrelated)
    source_before_clear = (
        await repository.refresh("alice")
        if source_scope == "user"
        else await repository.refresh_project("alice", "alice/api")
    )
    if source_scope == "user":
        await repository.clear_project("alice", "alice/api")
        source_after_clear = await repository.get("alice")
        sql = "SELECT overrides FROM agent_user_memories WHERE user_id = 'alice'"
    else:
        await repository.clear("alice")
        source_after_clear = await repository.get_project("alice", "alice/api")
        sql = (
            "SELECT overrides FROM agent_project_memories "
            "WHERE user_id = 'alice' AND repo = 'alice/api'"
        )
    assert source_after_clear == source_before_clear
    async with pool.acquire() as conn:
        overrides = json.loads(await conn.fetchval(sql))
    assert moved["content"] not in str(overrides)
    tombstone = {"source_ids": moved["source_ids"], "memory": None}
    if native_guard:
        tombstone["native_source_job_id"] = moved["source_job_id"]
    assert tombstone in overrides
    await _create_job(store, user_id="alice", repo="alice/api", task_prompt="Add API tests.")
    source_after_refresh = (
        await repository.refresh("alice")
        if source_scope == "user"
        else await repository.refresh_project("alice", "alice/api")
    )
    assert any(memory["content"] == unrelated for memory in source_after_refresh["memories"])
    assert not any(memory["id"] == moved["id"] for memory in source_after_refresh["memories"])
    if native_guard:
        duplicate = await _create_job(store, user_id="alice", repo="alice/api", task_prompt=content)
        refreshed = (
            await repository.refresh("alice")
            if source_scope == "user"
            else await repository.refresh_project("alice", "alice/api")
        )
        restored = next(memory for memory in refreshed["memories"] if memory["id"] == moved["id"])
        assert restored["source_job_id"] == duplicate["id"]
        assert restored["content"] == content
