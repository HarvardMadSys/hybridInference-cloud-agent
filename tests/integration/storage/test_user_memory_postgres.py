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
from cloud_agent.user_memory import prompt_with_user_memory

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
