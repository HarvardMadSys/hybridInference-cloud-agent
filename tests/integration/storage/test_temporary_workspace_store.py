"""Temporary workspaces on real Postgres: stored, settled and listed without a repository.

A conversation whose ``agent_threads.repo`` is null starts from the fixed empty
commit, binds only its owner's global variables, never opens a workspace
snapshot (so nothing is ever published), and is the one project the lists can
select by its absence of a repository. Migration ``0071`` allows the null and
refuses to go back while such a conversation exists.
"""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    _reserve_shared_chat,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.agent_memory import MemoryTask
from cloud_agent.storage.repositories.environments import EnvironmentNotFound
from cloud_agent_shared.git.temporary_workspace import TEMPORARY_WORKSPACE_BASE_SHA

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_REPO = "example-org/example-repo"
_GLOBALS_ONLY = {
    "environment_id": None,
    "environment_id_provided": True,
    "environment_revision": 0,
    "global_revision": None,
    "overrides": [],
    "has_setup_override": False,
    "setup_override": None,
}


async def _temporary(store, **overrides):
    return await _create_job(store, repo=None, base_sha=TEMPORARY_WORKSPACE_BASE_SHA, **overrides)


async def test_a_temporary_workspace_is_stored_without_a_repository(store, pool):
    """The conversation and its environment binding both carry a null repository."""
    job = await _temporary(store, environment_selection=_GLOBALS_ONLY)

    assert job["repo"] is None
    assert job["base_sha"] == TEMPORARY_WORKSPACE_BASE_SHA
    async with pool.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT repo, environment_binding_id FROM agent_threads WHERE id = $1",
            job["thread_id"],
        )
        binding_repo = await conn.fetchval(
            "SELECT repo FROM agent_environment_bindings WHERE id = $1",
            thread["environment_binding_id"],
        )
    assert thread["repo"] is None
    assert thread["environment_binding_id"] is not None
    assert binding_repo is None


async def test_a_temporary_workspace_reads_and_writes_only_user_memory(store):
    """Without a repository, a live task cannot read or create project memories."""
    user = await store.agent_memory.add_memory("alice", None, "Reply in Chinese.")
    project = await store.agent_memory.add_memory("alice", _REPO, "Use pnpm.")
    await store.agent_memory.add_memory("bob", None, "Use tabs.")
    job = await _temporary(store, user_id="alice")
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None and claim["id"] == job["id"]
    task = MemoryTask(
        user_id="alice",
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    saved = await store.agent_memory.save_agent_memory(
        task, "Always label chart axes.", all_projects=False
    )
    assert saved["repo"] is None
    assert saved["source_job_ids"] == [job["id"]]
    memories = await store.agent_memory.task_memories("alice", None)
    assert memories["repo"] is None
    assert memories["project"] == []
    assert {memory["id"] for memory in memories["user"]} == {user["id"], saved["id"]}
    repository_memories = await store.agent_memory.list_memories("alice", _REPO)
    assert [memory["id"] for memory in repository_memories["memories"]] == [project["id"]]


async def test_a_temporary_workspace_cannot_bind_a_repository_environment(store, pool):
    """Environments belong to one repository; a temporary workspace names none."""
    with pytest.raises(EnvironmentNotFound, match="no repository environment"):
        await _temporary(store, environment_selection={**_GLOBALS_ONLY, "environment_id": "env_1"})

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_threads") == 0


async def test_a_temporary_workspace_settles_without_opening_a_snapshot(store, pool):
    """The turn's boundary is kept, but nothing is queued for capture or publication."""
    job, claim, sandbox = await _reserve_shared_chat(
        store, repo=None, base_sha=TEMPORARY_WORKSPACE_BASE_SHA
    )
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-temporary", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )

    settled = await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-temporary",
        idle_timeout_seconds=1800,
        base_sha=TEMPORARY_WORKSPACE_BASE_SHA,
    )

    assert settled is not None
    async with pool.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT snapshot_state, published_base_sha, workspace_epoch FROM agent_sandboxes "
            "WHERE id = $1",
            sandbox["sandbox_id"],
        )
        snapshots = await conn.fetchval(
            "SELECT count(*) FROM agent_sandbox_snapshots WHERE sandbox_id = $1",
            sandbox["sandbox_id"],
        )
    assert group["snapshot_state"] == "quiescent"
    assert group["published_base_sha"] == TEMPORARY_WORKSPACE_BASE_SHA
    assert group["workspace_epoch"] == 1
    assert snapshots == 0
    assert await store.snapshot.claim_due_sandbox_snapshot() is None


async def test_the_lists_select_temporary_workspaces_and_count_them_as_a_project(store):
    """``temporary`` keeps only them; facets and projects show them as the null project."""
    repository = await _create_job(store)
    temporary = await _temporary(store)
    user_id = repository["user_id"]

    jobs = await store.conversation_queries.list_jobs(user_id=user_id, temporary=True)
    conversations = await store.conversation_queries.list_conversations(
        user_id=user_id, temporary=True
    )
    sessions = await store.conversation_queries.list_sessions(user_id=user_id, temporary=True)
    repository_sessions = await store.conversation_queries.list_sessions(
        user_id=user_id, repo=_REPO
    )
    projects = await store.conversation_queries.list_projects(user_id=user_id)

    assert [job["id"] for job in jobs] == [temporary["id"]]
    assert [item["job"]["id"] for item in conversations["conversations"]] == [temporary["id"]]
    assert [session["job"]["id"] for session in sessions["sessions"]] == [temporary["id"]]
    assert sessions["total"] == 1
    assert sorted(sessions["projects"], key=lambda item: item["repo"] or "") == [
        {"repo": None, "count": 1},
        {"repo": _REPO, "count": 1},
    ]
    assert [session["job"]["id"] for session in repository_sessions["sessions"]] == [
        repository["id"]
    ]
    assert {project["repo"]: project["task_count"] for project in projects} == {
        None: 1,
        _REPO: 1,
    }


async def test_a_title_search_still_finds_a_temporary_workspace(store):
    """A null repository is not a reason to drop a title match from search."""
    temporary = await _temporary(store, task_prompt="plot the survey results")

    found = await store.conversation_queries.list_conversations(
        user_id=temporary["user_id"], q="survey"
    )

    assert [item["job"]["id"] for item in found["conversations"]] == [temporary["id"]]


async def test_the_migration_refuses_to_go_back_while_a_temporary_workspace_exists(store, pool):
    """0071's downgrade would have to delete owners' conversations, so it refuses."""
    config = _db_config()
    await _temporary(store)
    try:
        with pytest.raises(Exception, match="Delete temporary workspace conversations"):
            downgrade_to(config, "0070_memory_cutover")

        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        downgrade_to(config, "0070_memory_cutover")
        async with pool.acquire() as conn:
            nullable = {
                row["table_name"]: row["is_nullable"]
                for row in await conn.fetch(
                    "SELECT table_name, is_nullable FROM information_schema.columns "
                    "WHERE column_name = 'repo' "
                    "AND table_name IN ('agent_threads', 'agent_environment_bindings')"
                )
            }
        assert nullable == {"agent_threads": "NO", "agent_environment_bindings": "NO"}
    finally:
        apply_migrations(config)
