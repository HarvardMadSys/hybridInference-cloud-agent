"""The previous API must survive schema migration before its replacement.

The SQL below retains the old API's column names on purpose. Testing only the
current repositories missed the staging failure: they already spoke the new
schema, while the process left serving after a readiness timeout did not.
"""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_mcp_connection_store import (
    _OWNER,
    _connect,
    _create_job,
    _db_config,
    _permission_binding,
    pool as pool,
)
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# cb29c4a, storage/_sql/jobs.py and repositories/mcp.py: the projections used
# by the old conversation list/detail, composer config and Integrations APIs.
_OLD_JOB_READ = """
SELECT j.id, j.thread_id, j.user_id, j.task_prompt, j.state,
       t.repo, t.setup_script, t.execution_profile, t.mcp_servers
FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
WHERE j.user_id = $1 ORDER BY j.id
"""
_OLD_CONNECTION_READ = """
SELECT id, user_id, server, account_label, access_token_ciphertext,
       refresh_token_ciphertext, expires_at, created_at, updated_at
FROM agent_mcp_connections WHERE user_id = $1 ORDER BY server
"""


async def _old_create(conn, name, servers):
    """The old submit/fork path writes thread config before its job row."""
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO agent_threads
                (id, user_id, repo, title, title_source,
                 setup_script, execution_profile, mcp_servers)
            VALUES ($1, $2, 'example/repo', 'Task', 'fallback', NULL, 'trusted-v1', $3)
            """,
            f"thread-{name}",
            _OWNER,
            servers,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs
                (id, thread_id, turn_no, user_id, task_prompt, runtime, model)
            VALUES ($1, $2, 1, $3, 'Fix the test', 'codex', 'test-model')
            """,
            name,
            f"thread-{name}",
            _OWNER,
        )


@pytest.mark.parametrize("revision", ["0008_generic_mcp_connections", "head"])
async def test_pre_mcp_api_reads_and_creates_jobs_after_upgrade(pool, revision):
    """Both 0008's commit and the final schema keep the serving API usable."""
    config = _db_config()
    downgrade_to(config, "0007_attempt_turn_resources")
    async with pool.acquire() as conn:
        await _old_create(conn, "before", ["github"])
        connection_id = await conn.fetchval(
            "INSERT INTO agent_mcp_connections "
            "(user_id, server, account_label, access_token_ciphertext) "
            "VALUES ($1, 'github', 'octocat', 'legacy-ciphertext') RETURNING id",
            _OWNER,
        )
        assert (await conn.fetch(_OLD_JOB_READ, _OWNER))[0]["mcp_servers"] == ["github"]
        assert (await conn.fetch(_OLD_CONNECTION_READ, _OWNER))[0]["server"] == "github"

    apply_migrations(config, revision)
    async with pool.acquire() as conn:
        assert (await conn.fetch(_OLD_JOB_READ, _OWNER))[0]["mcp_servers"] == []
        connections = await conn.fetch(_OLD_CONNECTION_READ, _OWNER)
        assert [(row["id"], row["server"]) for row in connections] == [(connection_id, None)]
        assert connections[0]["access_token_ciphertext"] == "legacy-ciphertext"
        # Requests accepted by the old API during the host build remain queued
        # for the new generation, and can be listed immediately by the old one.
        await _old_create(conn, "during", [])
        jobs = await conn.fetch(_OLD_JOB_READ, _OWNER)
        assert [(row["id"], row["state"]) for row in jobs] == [
            ("before", "queued"),
            ("during", "queued"),
        ]
        binding = await conn.fetchrow(
            "SELECT connection_id, selection_pending FROM agent_mcp_thread_bindings "
            "WHERE thread_id = 'thread-before'"
        )
        assert dict(binding) == {"connection_id": connection_id, "selection_pending": True}

    # Finish the rollout: the current API can read both generations' jobs.
    apply_migrations(config)
    current = Storage(pool)
    for name in ("before", "during"):
        job = await current.conversation_queries.get_job(name)
        assert job["state"] == "queued"


async def test_forward_repair_preserves_new_connections_and_permissions(pool):
    """Repair the already-deployed 0010 shape, without rolling back user data."""
    config = _db_config()
    store = Storage(pool)
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    thread_id = job["thread_id"]
    await store.mcp.replace_mcp_bindings(
        thread_id=thread_id, bindings=[_permission_binding(connection, "docs")]
    )
    before_bindings = await store.mcp.list_mcp_bindings(thread_id=thread_id)
    before_connection = await store.mcp.get_mcp_connection(connection_id=connection["id"])

    # 0011's downgrade restores the exact missing-column shape of the shipped
    # 0010. The regression first proves that both old reads fail against it.
    downgrade_to(config, "0010_mcp_policy_revisions")
    async with pool.acquire() as conn:
        for sql in (_OLD_JOB_READ, _OLD_CONNECTION_READ):
            with pytest.raises(asyncpg.UndefinedColumnError):
                await conn.fetch(sql, _OWNER)

    apply_migrations(config)
    async with pool.acquire() as conn:
        assert (await conn.fetch(_OLD_JOB_READ, _OWNER))[0]["id"] == job["id"]
        assert (await conn.fetch(_OLD_CONNECTION_READ, _OWNER))[0]["server"] is None
        await _old_create(conn, "after-repair", [])

    repaired = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert repaired == before_connection
    assert await store.mcp.list_mcp_bindings(thread_id=thread_id) == before_bindings


async def test_compatibility_columns_cannot_authorize_or_delete_new_mcp_connections(pool):
    """Old provider-name access cannot bypass the current binding policies."""
    store = Storage(pool)
    job = await _create_job(store)
    first = await _connect(store, _OWNER, label="Work")
    second = await _connect(store, _OWNER, label="Personal")
    await store.mcp.replace_mcp_bindings(
        thread_id=job["thread_id"], bindings=[_permission_binding(first, "docs")]
    )
    before = await store.mcp.list_mcp_bindings(thread_id=job["thread_id"])

    async with pool.acquire() as conn:
        assert all(row["server"] is None for row in await conn.fetch(_OLD_CONNECTION_READ, _OWNER))
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_threads SET mcp_servers = ARRAY['github'] WHERE id = $1",
                job["thread_id"],
            )
        with pytest.raises(asyncpg.GeneratedAlwaysError):
            await conn.execute(
                "UPDATE agent_mcp_connections SET server = 'github' WHERE id = $1", first["id"]
            )
        assert (
            await conn.fetchval(
                "DELETE FROM agent_mcp_connections WHERE user_id = $1 AND server = 'github' RETURNING id",
                _OWNER,
            )
            is None
        )

    assert await store.mcp.list_mcp_bindings(thread_id=job["thread_id"]) == before
    assert {row["id"] for row in await store.mcp.list_mcp_connections(user_id=_OWNER)} == {
        first["id"],
        second["id"],
    }
