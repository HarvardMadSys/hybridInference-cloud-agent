"""Durable memories: owner-only rows and the limits that bound them."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
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
    AgentMemoryTaskEnded,
    MemoryTask,
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


async def test_an_owner_adding_an_agents_rule_makes_it_their_own(store, pool):
    """The rule then counts as written by the owner, and is never archived for room."""
    await _fill(pool, "agent")
    added = await store.agent_memory.add_memory("alice", None, "rule 0")
    assert added["id"] == "agent-0"
    assert added["origin"] == "manual"
    assert added["version"] == 1
    await store.agent_memory.add_memory("alice", None, "Newest rule")
    scope = await store.agent_memory.list_memories("alice", None)
    assert "agent-0" not in [memory["id"] for memory in scope["archived"]]
    assert [memory["id"] for memory in scope["archived"]] == ["agent-1"]


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
    """A task with a running attempt, and the fence its token would carry."""
    job = await _create_job(store, user_id=user_id, repo=repo)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == job["id"]
    task = MemoryTask(
        user_id=user_id,
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )
    return job, task


async def test_agents_save_to_the_tasks_repository_or_every_project(store, pool):
    memories = store.agent_memory
    job, task = await _job(store)
    saved = await memories.save_agent_memory(task, "Use pnpm.", all_projects=False)
    assert (saved["repo"], saved["origin"], saved["source_job_ids"]) == (
        "owner/api",
        "agent",
        [job["id"]],
    )
    everywhere = await memories.save_agent_memory(task, "Reply in Chinese.", all_projects=True)
    assert everywhere["repo"] is None

    again, again_task = await _job(store)
    repeated = await memories.save_agent_memory(again_task, "use PNPM", all_projects=False)
    assert repeated["id"] == saved["id"]
    assert repeated["source_job_ids"] == [job["id"], again["id"]]

    await memories.set_memory_enabled("alice", "owner/api", False)
    with pytest.raises(AgentMemoryDisabled):
        await memories.save_agent_memory(task, "Use tabs.", all_projects=False)
    # Another owner cannot borrow this task's attempt.
    bobs_task = MemoryTask("bob", task.job_id, task.attempt_id, task.lease_generation)
    with pytest.raises(AgentMemoryTaskEnded):
        await memories.save_agent_memory(bobs_task, "Use tabs.", all_projects=True)
    with pytest.raises(ValueError):
        await memories.save_agent_memory(task, "token=secretvalue", all_projects=True)


async def test_an_agent_replaces_any_visible_memory_and_the_owner_can_undo_it(store, pool):
    memories = store.agent_memory
    _, task = await _job(store)
    written = await memories.add_memory("alice", "owner/api", "Use npm.")
    replaced = await memories.replace_agent_memory(task, written["id"], "Use pnpm.")
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

    unchanged = await memories.replace_agent_memory(task, replaced["id"], "use pnpm")
    assert unchanged["id"] == replaced["id"]

    archived = await memories.archive_agent_memory(task, replaced["id"])
    assert archived["status"] == "archived"
    with pytest.raises(AgentMemoryNotFound):
        await memories.archive_agent_memory(task, replaced["id"])


async def test_an_agent_cannot_touch_another_repository_or_owner(store, pool):
    memories = store.agent_memory
    job, task = await _job(store)
    other_repo = await memories.add_memory("alice", "owner/other", "Use tabs.")
    bobs = await memories.add_memory("bob", None, "Use spaces.")
    for memory_id in (other_repo["id"], bobs["id"], "missing"):
        with pytest.raises(AgentMemoryNotFound):
            await memories.replace_agent_memory(task, memory_id, "Use yarn.")
        with pytest.raises(AgentMemoryNotFound):
            await memories.archive_agent_memory(task, memory_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", job["thread_id"]
        )
    with pytest.raises(AgentMemoryTaskEnded):
        await memories.save_agent_memory(task, "Use pnpm.", all_projects=True)


async def test_a_task_reads_its_scopes_newest_first_and_marks_only_what_it_used(store, pool):
    memories = store.agent_memory
    older = await memories.add_memory("alice", "owner/api", "Use pnpm.")
    newer = await memories.add_memory("alice", "owner/api", "Run make check.")
    user = await memories.add_memory("alice", None, "Reply in Chinese.")
    await memories.add_memory("alice", "owner/other", "Use tabs.")
    gone = await memories.add_memory("alice", None, "Old rule.")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_memories SET status = 'archived', archived_at = NOW() WHERE id = $1",
            gone["id"],
        )
        # The oldest rule was edited last, so it is the one to keep first.
        await conn.execute(
            "UPDATE agent_memories SET updated_at = NOW() + interval '1 minute' WHERE id = $1",
            older["id"],
        )
    read = await memories.task_memories("alice", "owner/api")
    assert read["repo"] == "owner/api"
    assert [m["id"] for m in read["project"]] == [older["id"], newer["id"]]
    assert [m["id"] for m in read["user"]] == [user["id"]]

    await memories.mark_memories_used("alice", [older["id"]])
    await memories.mark_memories_used("bob", [newer["id"]])
    async with pool.acquire() as conn:
        marked = await conn.fetch("SELECT id FROM agent_memories WHERE last_used_at IS NOT NULL")
    assert [row["id"] for row in marked] == [older["id"]]

    assert (await memories.task_memories("alice", None))["project"] == []
    await memories.set_memory_enabled("alice", None, False)
    assert (await memories.task_memories("alice", "owner/api"))["user"] == []


async def test_a_task_stopped_after_its_token_was_accepted_cannot_write(store, pool):
    memories = store.agent_memory
    job, task = await _job(store)
    saved = await memories.save_agent_memory(task, "Use pnpm.", all_projects=False)
    # The owner presses Stop while the write is in flight: the stop holds the
    # job row, the write waits for it, and then sees the task is stopping.
    async with pool.acquire() as conn:
        stop = conn.transaction()
        await stop.start()
        await conn.execute("UPDATE agent_jobs SET cancel_requested = TRUE WHERE id = $1", job["id"])
        write = asyncio.create_task(
            memories.save_agent_memory(task, "Use yarn.", all_projects=False)
        )
        await asyncio.sleep(0.3)
        assert not write.done()
        await stop.commit()
    with pytest.raises(AgentMemoryTaskEnded):
        await write
    for change in (
        memories.replace_agent_memory(task, saved["id"], "Use yarn."),
        memories.archive_agent_memory(task, saved["id"]),
    ):
        with pytest.raises(AgentMemoryTaskEnded):
            await change
    stale = MemoryTask(task.user_id, task.job_id, task.attempt_id, task.lease_generation + 1)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET cancel_requested = FALSE WHERE id = $1", job["id"]
        )
    with pytest.raises(AgentMemoryTaskEnded):
        await memories.save_agent_memory(stale, "Use yarn.", all_projects=False)
    assert [
        m["content"] for m in (await memories.list_memories("alice", "owner/api"))["memories"]
    ] == ["Use pnpm."]


async def test_switches_persist_per_scope_and_projects_are_listed(store, pool):
    memories = store.agent_memory
    await _create_job(store, user_id="alice", repo="owner/api")
    await memories.add_memory("alice", "owner/kept", "Use tabs.")
    paused = await memories.set_memory_enabled("alice", "owner/api", False)
    assert paused["enabled"] is False and paused["repo"] == "owner/api"
    assert (await memories.list_memories("alice", None))["enabled"] is True
    assert (await memories.set_memory_enabled("alice", "owner/api", True))["enabled"] is True
    await memories.set_memory_enabled("alice", "owner/switch-only", False)
    assert await memories.memory_projects("alice") == [
        "owner/api",
        "owner/kept",
        "owner/switch-only",
    ]
    assert await memories.memory_projects("bob") == []


async def test_insights_read_only_the_owners_own_recent_requests(store, pool):
    await _create_job(store, user_id="alice", task_prompt="First request")
    await _create_job(store, user_id="alice", task_prompt="Second request")
    await _create_job(store, user_id="bob", task_prompt="Not alice's")
    assert await store.work_insights.recent_requests("alice") == [
        "Second request",
        "First request",
    ]
    assert await store.work_insights.recent_requests("alice", 1) == ["Second request"]


async def test_the_cutover_copies_switches_and_the_preferences_tasks_received(store, pool):
    config = _db_config()

    def entry(content, *, kind="preference", scope=None, edited=False, repo=None):
        value = {"id": content, "kind": kind, "content": content, "source_job_id": "job-1"}
        if scope:
            value["scope"] = scope
        if repo:
            value["source_repo"] = repo
        if edited:
            value["user_edited"] = True
        return value

    user_view = [
        entry("Always reply in Chinese.", scope="user"),
        entry("Never use emoji.", edited=True),
        entry("always reply in chinese", scope="user"),
        entry("Fix the login page", kind="task"),
        entry("Always use pnpm.", scope="repo", repo="owner/api"),
        entry("x" * 241, scope="user"),
    ]
    project_view = [
        entry("Always use pnpm.", scope="repo", repo="owner/api"),
        entry("Never force-push.", scope="user", repo="owner/api"),
    ]
    try:
        downgrade_to(config, "0069_agent_memories")
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_memories")
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, enabled, portrait, memories) "
                "VALUES ('alice', FALSE, '{}'::jsonb, $1::jsonb)",
                json.dumps(user_view),
            )
            await conn.execute(
                "INSERT INTO agent_project_memories (user_id, repo, enabled, portrait, memories) "
                "VALUES ('alice', 'owner/api', TRUE, '{}'::jsonb, $1::jsonb)",
                json.dumps(project_view),
            )
        apply_migrations(config, "0070_memory_cutover")
        # A second pass after a downgrade copies nothing twice.
        downgrade_to(config, "0069_agent_memories")
        apply_migrations(config, "0070_memory_cutover")
        async with pool.acquire() as conn:
            settings = await conn.fetch(
                "SELECT repo, enabled FROM agent_memory_settings WHERE user_id = 'alice' "
                "ORDER BY repo"
            )
            copied = await conn.fetch(
                "SELECT repo, content, origin, source_job_ids, description FROM agent_memories "
                "WHERE user_id = 'alice' ORDER BY repo NULLS FIRST, content"
            )
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_user_memories")
            await conn.execute("DELETE FROM agent_project_memories")
        apply_migrations(config)
    assert [(row["repo"], row["enabled"]) for row in settings] == [("", False), ("owner/api", True)]
    assert [(row["repo"], row["content"], row["origin"]) for row in copied] == [
        (None, "Always reply in Chinese.", "agent"),
        (None, "Never use emoji.", "manual"),
        ("owner/api", "Always use pnpm.", "agent"),
    ]
    assert all(list(row["source_job_ids"]) == ["job-1"] for row in copied)


_OLD_TABLES = (
    "agent_user_memory_edits",
    "agent_project_memory_edits",
    "agent_user_memories",
    "agent_project_memories",
)


async def test_the_retired_memory_tables_are_dropped_and_come_back_empty(store, pool):
    config = _db_config()

    async def present(conn):
        return [await conn.fetchval("SELECT to_regclass($1)::text", table) for table in _OLD_TABLES]

    try:
        async with pool.acquire() as conn:
            assert await present(conn) == [None] * 4
        downgrade_to(config, "0071_temporary_workspaces")
        async with pool.acquire() as conn:
            assert await present(conn) == list(_OLD_TABLES)
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait) VALUES ('alice', '{}'::jsonb)"
            )
            await conn.execute(
                "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id) "
                "VALUES ('alice', 'm1', 'job-1')"
            )
        apply_migrations(config)
        async with pool.acquire() as conn:
            assert await present(conn) == [None] * 4
            # The durable memory tables are untouched.
            assert await conn.fetchval("SELECT to_regclass('agent_memory_settings')") is not None
        downgrade_to(config, "0071_temporary_workspaces")
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM agent_user_memories") == 0
            assert await conn.fetchval("SELECT count(*) FROM agent_user_memory_edits") == 0
    finally:
        apply_migrations(config)
