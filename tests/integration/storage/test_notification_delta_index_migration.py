"""Notification delta pagination keeps an index aligned with its full keyset."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0103_terminal_resume_keyset"
_THIS = "0104_notification_change_keyset"
_PREVIOUS_INDEX = "idx_agent_notification_changes_owner_xid"
_KEYSET_INDEX = "idx_agent_notification_changes_owner_kind_xid"


async def _index(pool, name: str) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT i.indisvalid,pg_get_indexdef(i.indexrelid) AS definition "
            "FROM pg_index i WHERE i.indexrelid=to_regclass($1)",
            name,
        )


async def test_keyset_index_replaces_and_restores_the_previous_access_path(pool):
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        previous = await _index(pool, _PREVIOUS_INDEX)
        assert previous is not None and previous["indisvalid"] is True
        assert await _index(pool, _KEYSET_INDEX) is None

        apply_migrations(config, _THIS)
        keyset = await _index(pool, _KEYSET_INDEX)
        assert keyset is not None and keyset["indisvalid"] is True
        assert keyset["definition"].endswith("USING btree (user_id, kind, change_xid, entity_id)")
        assert await _index(pool, _PREVIOUS_INDEX) is None

        downgrade_to(config, _BEFORE)
        restored = await _index(pool, _PREVIOUS_INDEX)
        assert restored is not None and restored["definition"] == previous["definition"]
        assert await _index(pool, _KEYSET_INDEX) is None
    finally:
        apply_migrations(config)


async def test_interrupted_keyset_index_build_is_replaced(pool):
    """A failed concurrent build leaves an invalid name that a retry must replace."""
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_notification_changes (
                    kind,entity_id,user_id,change_xid,changed_at,terminal
                ) VALUES
                    ('job','retry-a','retry-owner',pg_current_xact_id(),NOW(),FALSE),
                    ('job','retry-b','retry-owner',pg_current_xact_id(),NOW(),FALSE)
                """
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {_KEYSET_INDEX} "
                    "ON agent_notification_changes (user_id)"
                )
        invalid = await _index(pool, _KEYSET_INDEX)
        assert invalid is not None and invalid["indisvalid"] is False
        previous = await _index(pool, _PREVIOUS_INDEX)
        assert previous is not None and previous["indisvalid"] is True

        apply_migrations(config, _THIS)

        rebuilt = await _index(pool, _KEYSET_INDEX)
        assert rebuilt is not None and rebuilt["indisvalid"] is True
        assert rebuilt["definition"].endswith("USING btree (user_id, kind, change_xid, entity_id)")
        assert await _index(pool, _PREVIOUS_INDEX) is None
    finally:
        apply_migrations(config)
