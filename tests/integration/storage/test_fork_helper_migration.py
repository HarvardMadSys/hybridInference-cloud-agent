"""Retained groups record historical Fork support across the image-label change."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _reserve_retained_chat,
    _reserve_shared_chat,
)
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0025_github_user_tokens"
_THIS = "0026_fork_helper_version"


async def _groups(conn) -> list[dict]:
    return [dict(row) for row in await conn.fetch("SELECT * FROM agent_sandboxes ORDER BY id")]


async def test_an_existing_group_records_its_at_anchor_helper_and_nothing_else_changes(store):
    """Both sides of #351 required the same historical-Fork implementation."""
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
        assert await _groups(conn) == [{**row, "fork_helper_version": "2"} for row in before]
        # An image without the label is recorded as null, never as "".
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE agent_sandboxes SET fork_helper_version = ''")
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
    """Converted groups may predate historical Fork; other groups can be backfilled."""
    _job, _claim, archived = await _reserve_shared_chat(store)
    _other_job, _other_claim, current = await _reserve_retained_chat(store)
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
                    "SELECT fork_helper_version FROM agent_sandboxes WHERE id = $1",
                    archived["sandbox_id"],
                )
                is None
            )
            assert (
                await conn.fetchval(
                    "SELECT fork_helper_version FROM agent_sandboxes WHERE id = $1",
                    current["sandbox_id"],
                )
                == "2"
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
