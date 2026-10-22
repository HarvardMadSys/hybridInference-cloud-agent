"""The online index can recover an interrupted build and downgrade without data loss."""

import asyncpg
import pytest
from tests.integration.storage.test_conversation_queries_store import db as db, store as store
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.integration.storage.test_thread_history_pages import history
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_rebuilds_an_invalid_index_and_preserves_messages_on_downgrade(store, db):
    root = await history(store, db, 2)
    config = _db_config()
    index = "idx_agent_thread_messages_job_order"
    try:
        downgrade_to(config, "0092_targeted_work_wakes")
        async with db.acquire() as conn:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {index} ON agent_thread_messages (thread_id)"
                )
            assert (
                await conn.fetchval(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass($1)", index
                )
                is False
            )
        apply_migrations(config)
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT indisvalid,pg_get_indexdef(indexrelid) AS definition FROM pg_index WHERE indexrelid=to_regclass($1)",
                index,
            )
            assert row["indisvalid"] is True
            assert "job_id" in row["definition"] and "mid_turn" in row["definition"]
        downgrade_to(config, "0092_targeted_work_wakes")
        async with db.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass($1)", index) is None
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_thread_messages WHERE thread_id=$1",
                    root["thread_id"],
                )
                == 6
            )
    finally:
        apply_migrations(config)
