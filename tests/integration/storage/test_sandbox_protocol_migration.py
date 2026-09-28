"""The sandbox-protocol revision: groups this schema created are recorded as v2."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import _reserve_shared_chat
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0025_github_user_tokens"
_THIS = "0026_sandbox_protocol"


async def _groups(conn) -> list[dict]:
    return [dict(row) for row in await conn.fetch("SELECT * FROM agent_sandboxes ORDER BY id")]


async def test_an_existing_group_is_recorded_as_v2_and_nothing_else_changes(store):
    """Only a v2 image could ever create a group, so the backfill states a fact."""
    await _reserve_shared_chat(store)
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        before = await _groups(conn)
    finally:
        await conn.close()
    assert before

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        assert await _groups(conn) == [{**row, "shared_sandbox_protocol": "v2"} for row in before]
        # An image without the label is recorded as null, never as "".
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE agent_sandboxes SET shared_sandbox_protocol = ''")
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert await _groups(conn) == before
    finally:
        await conn.close()
    apply_migrations(config)


async def test_a_group_copied_from_an_old_chain_database_stays_unrecorded(store):
    """A converted copy's groups may predate v2; they keep the exact-image rule."""
    _job, _claim, archived = await _reserve_shared_chat(store)
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        # What scripts/convert_legacy_database_copy.py leaves behind: the old
        # schema renamed, holding the rows it copied into the new one.
        await conn.execute("CREATE SCHEMA legacy_archive")
        await conn.execute("CREATE TABLE legacy_archive.agent_sandboxes (id TEXT PRIMARY KEY)")
        await conn.execute(
            "INSERT INTO legacy_archive.agent_sandboxes (id) VALUES ($1)",
            archived["sandbox_id"],
        )
    finally:
        await conn.close()
    try:
        apply_migrations(config, _THIS)
        conn = await asyncpg.connect(**config)
        try:
            assert (
                await conn.fetchval(
                    "SELECT shared_sandbox_protocol FROM agent_sandboxes WHERE id = $1",
                    archived["sandbox_id"],
                )
                is None
            )
        finally:
            await conn.close()
    finally:
        conn = await asyncpg.connect(**config)
        try:
            await conn.execute("DROP SCHEMA IF EXISTS legacy_archive CASCADE")
        finally:
            await conn.close()
    apply_migrations(config)
