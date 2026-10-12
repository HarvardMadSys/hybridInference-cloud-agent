"""Sandbox plugin selections upgrade over existing chats and round-trip safely."""

from __future__ import annotations

import json

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0047_user_memory"
_THIS = "0048_sandbox_plugins"


async def test_plugin_upgrade_defaults_and_rollback_preserve_existing_chats(store):
    """Older writers can omit plugins; rollback removes only the new column."""
    job = await _create_job(store, setup_script="npm ci", metadata={"origin": "test"})
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        original_memory = dict(
            await conn.fetchrow(
                "INSERT INTO agent_user_memories (user_id, portrait, memories) "
                "VALUES ($1, $2::jsonb, $3::jsonb) RETURNING *",
                job["user_id"],
                json.dumps({"summary": "Prefers readable API tests."}),
                json.dumps([{"text": "Keep migration assertions precise."}]),
            )
        )
        original_thread = dict(
            await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"])
        )
        original_job = dict(
            await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"])
        )
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        assert (
            dict(
                await conn.fetchrow(
                    "SELECT * FROM agent_user_memories WHERE user_id = $1", job["user_id"]
                )
            )
            == original_memory
        )
        upgraded_thread = dict(
            await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"])
        )
        assert json.loads(upgraded_thread.pop("plugins")) == []
        assert upgraded_thread == original_thread
        assert dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"])) == (
            original_job
        )
        column = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'agent_threads' "
            "AND column_name = 'plugins'"
        )
        assert dict(column) == {
            "data_type": "jsonb",
            "is_nullable": "NO",
            "column_default": "'[]'::jsonb",
        }
        # This is the INSERT the preceding release's writer can still issue.
        legacy_thread = dict(
            await conn.fetchrow(
                "INSERT INTO agent_threads (id, user_id, repo, title, execution_profile) "
                "VALUES ('athread_legacy_plugins', 'user-1', 'owner/repo', 'Legacy chat', 'trusted-v1') "
                "RETURNING *"
            )
        )
        assert json.loads(legacy_thread.pop("plugins")) == []
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_threads SET plugins = '{}'::jsonb WHERE id = $1", job["thread_id"]
            )
        await conn.execute(
            "UPDATE agent_threads SET plugins = $2::jsonb WHERE id = $1",
            legacy_thread["id"],
            json.dumps([{"plugin": "reviewer@team-tools", "marketplace": "owner/repo"}]),
        )
        legacy_thread = dict(
            await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", legacy_thread["id"])
        )
        assert json.loads(legacy_thread.pop("plugins")) == [
            {"plugin": "reviewer@team-tools", "marketplace": "owner/repo"}
        ]
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert (
            dict(
                await conn.fetchrow(
                    "SELECT * FROM agent_user_memories WHERE user_id = $1", job["user_id"]
                )
            )
            == original_memory
        )
        assert (
            dict(await conn.fetchrow("SELECT * FROM agent_threads WHERE id = $1", job["thread_id"]))
            == original_thread
        )
        assert dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job["id"])) == (
            original_job
        )
        assert (
            dict(
                await conn.fetchrow(
                    "SELECT * FROM agent_threads WHERE id = $1", legacy_thread["id"]
                )
            )
            == legacy_thread
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_threads' "
                "AND column_name = 'plugins'"
            )
            == 0
        )
    finally:
        await conn.close()
    apply_migrations(config)
