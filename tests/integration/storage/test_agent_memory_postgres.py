"""Durable memories: owner-only rows and the limits that bound them."""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.agent_memory import (
    ACTIVE_LIMIT,
    AgentMemoryConflict,
    AgentMemoryDisabled,
    AgentMemoryFull,
    AgentMemoryNotFound,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _fill(pool, origin: str) -> None:
    async with pool.acquire() as conn:
        for n in range(ACTIVE_LIMIT):
            await conn.execute(
                "INSERT INTO agent_memories (id, user_id, content, description, origin, "
                "created_at, updated_at) VALUES ($1, 'alice', $2, $2, $3, "
                "NOW() - make_interval(mins => $4), NOW() - make_interval(mins => $4))",
                f"{origin}-{n}",
                f"Rule {n}",
                origin,
                ACTIVE_LIMIT - n,
            )


async def test_a_full_scope_archives_its_least_recently_used_agent_memory(store, pool):
    await _fill(pool, "agent")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_memories SET last_used_at = NOW() WHERE id = 'agent-0'")
    saved = await store.agent_memory.add_memory("alice", None, "Newest rule")
    scope = await store.agent_memory.list_memories("alice", None)
    assert len(scope["memories"]) == ACTIVE_LIMIT
    assert [memory["id"] for memory in scope["archived"]] == ["agent-1"]
    assert scope["archived"][0]["archived_at"] is not None
    assert saved["content"] == "Newest rule"


async def test_memories_the_owner_wrote_are_never_archived_to_make_room(store, pool):
    await _fill(pool, "manual")
    with pytest.raises(AgentMemoryFull):
        await store.agent_memory.add_memory("alice", None, "Another rule")
    scope = await store.agent_memory.list_memories("alice", None)
    assert len(scope["memories"]) == ACTIVE_LIMIT and scope["archived"] == []


async def test_owner_changes_are_versioned_scoped_and_owner_only(store, pool):
    memories = store.agent_memory
    added = await memories.add_memory("alice", None, "  Reply in Chinese ")
    assert added["content"] == "Reply in Chinese" and added["origin"] == "manual"
    assert (await memories.add_memory("alice", None, "reply in chinese."))["id"] == added["id"]
    with pytest.raises(ValueError):
        await memories.add_memory("alice", None, "Use token=privatevalue")

    moved = await memories.update_memory(
        "alice", added["id"], content="Use pnpm", repo="owner/api", expected_version=0
    )
    assert moved["repo"] == "owner/api" and moved["version"] == 1
    assert (await memories.list_memories("alice", None))["memories"] == []
    assert [m["id"] for m in (await memories.list_memories("alice", "owner/api"))["memories"]] == [
        added["id"]
    ]
    with pytest.raises(AgentMemoryConflict):
        await memories.update_memory(
            "alice", added["id"], content="Use npm", repo=None, expected_version=0
        )
    with pytest.raises(AgentMemoryNotFound):
        await memories.update_memory("bob", added["id"], content="x", repo=None, expected_version=1)
    with pytest.raises(AgentMemoryNotFound):
        await memories.delete_memory("bob", added["id"], expected_version=1)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_memories SET status = 'archived', archived_at = NOW(), "
            "version = version + 1 WHERE id = $1",
            added["id"],
        )
    with pytest.raises(AgentMemoryNotFound):
        await memories.update_memory(
            "alice", added["id"], content="x", repo=None, expected_version=2
        )
    restored = await memories.restore_memory("alice", added["id"], expected_version=2)
    assert restored["status"] == "active" and restored["archived_at"] is None
    with pytest.raises(AgentMemoryNotFound):
        await memories.restore_memory("alice", added["id"], expected_version=3)
    with pytest.raises(AgentMemoryConflict):
        await memories.delete_memory("alice", added["id"], expected_version=0)
    await memories.delete_memory("alice", added["id"], expected_version=3)
    assert (await memories.list_memories("alice", "owner/api"))["memories"] == []

    await memories.add_memory("alice", None, "Keep for user")
    await memories.add_memory("alice", "owner/api", "Clear for project")
    await memories.add_memory("bob", "owner/api", "Keep for bob")
    await memories.clear_memories("alice", "owner/api")
    assert (await memories.list_memories("alice", "owner/api"))["memories"] == []
    assert len((await memories.list_memories("alice", None))["memories"]) == 1
    assert len((await memories.list_memories("bob", "owner/api"))["memories"]) == 1


async def test_purge_forgets_expired_archives_only(store, pool):
    old = await store.agent_memory.add_memory("alice", None, "Old archive")
    recent = await store.agent_memory.add_memory("alice", None, "Recent archive")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_memories SET status = 'archived', archived_at = NOW() - "
            "CASE WHEN id = $1 THEN interval '91 days' ELSE interval '89 days' END",
            old["id"],
        )
    await store.agent_memory.purge_memories()
    archived = (await store.agent_memory.list_memories("alice", None))["archived"]
    assert [memory["id"] for memory in archived] == [recent["id"]]


async def test_migration_round_trip(store, pool):
    config = _db_config()
    try:
        downgrade_to(config, "0068_sandbox_checkpoints")
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass('agent_memories')") is None
        apply_migrations(config, "0069_agent_memories")
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM agent_memories") == 0
            with pytest.raises(Exception, match="ck_agent_memories_archived"):
                await conn.execute(
                    "INSERT INTO agent_memories (id, user_id, content, description, origin, "
                    "status) VALUES ('x', 'alice', 'Rule', 'Rule', 'manual', 'archived')"
                )
            with pytest.raises(Exception, match="ck_agent_memories_origin"):
                await conn.execute(
                    "INSERT INTO agent_memories (id, user_id, content, description, origin) "
                    "VALUES ('z', 'alice', 'Rule', 'Rule', 'inferred')"
                )
            with pytest.raises(Exception, match="ck_agent_memories_repo"):
                await conn.execute(
                    "INSERT INTO agent_memories (id, user_id, repo, content, description, "
                    "origin) VALUES ('y', 'alice', ' ', 'Rule', 'Rule', 'manual')"
                )
    finally:
        apply_migrations(config)


async def _job(store, *, user_id="alice", repo="owner/api"):
    return await _create_job(store, user_id=user_id, repo=repo)


async def test_agents_save_to_the_tasks_repository_or_every_project(store, pool):
    memories = store.agent_memory
    job = await _job(store)
    saved = await memories.save_agent_memory("alice", job["id"], "Use pnpm.", all_projects=False)
    assert (saved["repo"], saved["origin"], saved["source_job_ids"]) == (
        "owner/api",
        "agent",
        [job["id"]],
    )
    everywhere = await memories.save_agent_memory(
        "alice", job["id"], "Reply in Chinese.", all_projects=True
    )
    assert everywhere["repo"] is None

    again = await _job(store)
    repeated = await memories.save_agent_memory(
        "alice", again["id"], "use PNPM", all_projects=False
    )
    assert repeated["id"] == saved["id"]
    assert repeated["source_job_ids"] == [job["id"], again["id"]]

    await store.user_memory.set_project_enabled("alice", "owner/api", False)
    with pytest.raises(AgentMemoryDisabled):
        await memories.save_agent_memory("alice", job["id"], "Use tabs.", all_projects=False)
    with pytest.raises(AgentMemoryNotFound):
        await memories.save_agent_memory("bob", job["id"], "Use tabs.", all_projects=True)
    with pytest.raises(ValueError):
        await memories.save_agent_memory("alice", job["id"], "token=secretvalue", all_projects=True)


async def test_an_agent_replaces_any_visible_memory_and_the_owner_can_undo_it(store, pool):
    memories = store.agent_memory
    job = await _job(store)
    written = await memories.add_memory("alice", "owner/api", "Use npm.")
    replaced = await memories.replace_agent_memory("alice", job["id"], written["id"], "Use pnpm.")
    assert (replaced["repo"], replaced["origin"], replaced["content"]) == (
        "owner/api",
        "agent",
        "Use pnpm.",
    )
    scope = await memories.list_memories("alice", "owner/api")
    assert [m["id"] for m in scope["memories"]] == [replaced["id"]]
    [old] = scope["archived"]
    assert old["id"] == written["id"]
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT superseded_by FROM agent_memories WHERE id = $1", written["id"]
            )
            == replaced["id"]
        )
    # The owner undoes the change by restoring the archived text.
    restored = await memories.restore_memory(
        "alice", written["id"], expected_version=old["version"]
    )
    assert restored["status"] == "active"

    unchanged = await memories.replace_agent_memory("alice", job["id"], replaced["id"], "use pnpm")
    assert unchanged["id"] == replaced["id"]

    archived = await memories.archive_agent_memory("alice", job["id"], replaced["id"])
    assert archived["status"] == "archived"
    with pytest.raises(AgentMemoryNotFound):
        await memories.archive_agent_memory("alice", job["id"], replaced["id"])


async def test_an_agent_cannot_touch_another_repository_or_owner(store, pool):
    memories = store.agent_memory
    job = await _job(store)
    other_repo = await memories.add_memory("alice", "owner/other", "Use tabs.")
    bobs = await memories.add_memory("bob", None, "Use spaces.")
    for memory_id in (other_repo["id"], bobs["id"], "missing"):
        with pytest.raises(AgentMemoryNotFound):
            await memories.replace_agent_memory("alice", job["id"], memory_id, "Use yarn.")
        with pytest.raises(AgentMemoryNotFound):
            await memories.archive_agent_memory("alice", job["id"], memory_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", job["thread_id"]
        )
    with pytest.raises(AgentMemoryNotFound):
        await memories.save_agent_memory("alice", job["id"], "Use pnpm.", all_projects=True)


async def test_a_task_uses_its_repository_and_user_memories_and_marks_them(store, pool):
    memories = store.agent_memory
    project = await memories.add_memory("alice", "owner/api", "Use pnpm.")
    user = await memories.add_memory("alice", None, "Reply in Chinese.")
    await memories.add_memory("alice", "owner/other", "Use tabs.")
    gone = await memories.add_memory("alice", None, "Old rule.")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_memories SET status = 'archived', archived_at = NOW() WHERE id = $1",
            gone["id"],
        )
    used = await memories.use_memories("alice", "owner/api")
    assert used["repo"] == "owner/api"
    assert [m["id"] for m in used["project"]] == [project["id"]]
    assert [m["id"] for m in used["user"]] == [user["id"]]
    async with pool.acquire() as conn:
        marked = await conn.fetchval(
            "SELECT count(*) FROM agent_memories WHERE last_used_at IS NOT NULL"
        )
    assert marked == 2

    assert (await memories.use_memories("alice", None))["project"] == []
    await store.user_memory.set_enabled("alice", False)
    assert (await memories.use_memories("alice", "owner/api"))["user"] == []
