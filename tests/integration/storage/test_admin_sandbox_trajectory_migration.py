"""The sandbox trajectory migration makes its event ledger append-only."""

from __future__ import annotations

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.integration.storage.test_agent_job_store import (
    _reserve_shared_chat,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import REPO, apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BRANCH_POINT = "0045_admin_sandbox_deletion"
_BEFORE = "0056_cloudlab_autoscaling"
_THIS = "0057_admin_sandbox_trajectory"
_COMMITTED_HEADS = (
    pytest.param("0046_admin_sandbox_trajectory", True, id="trajectory-head"),
    pytest.param("0056_cloudlab_autoscaling", False, id="mainline-head"),
)


def _migration_head() -> str:
    """Return the sole repository head from an Alembic configuration."""
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


async def test_sandbox_events_reject_every_destructive_table_mutation(pool, store):
    """UPDATE, DELETE, and statement-level TRUNCATE all preserve the ledger."""
    _, _, sandbox = await _reserve_shared_chat(store)

    async with pool.acquire() as conn:
        event_id = await conn.fetchval(
            "SELECT id FROM agent_sandbox_events WHERE sandbox_id = $1 ORDER BY id LIMIT 1",
            sandbox["sandbox_id"],
        )
        assert event_id is not None
        original_count = await conn.fetchval("SELECT COUNT(*) FROM agent_sandbox_events")

        statements = (
            ("UPDATE agent_sandbox_events SET event_type = 'rewritten' WHERE id = $1", event_id),
            ("DELETE FROM agent_sandbox_events WHERE id = $1", event_id),
            ("TRUNCATE TABLE agent_sandbox_events",),
            ("TRUNCATE TABLE agent_sandboxes CASCADE",),
        )
        for statement, *arguments in statements:
            with pytest.raises(asyncpg.RaiseError, match="agent_sandbox_events is append-only"):
                async with conn.transaction():
                    await conn.execute(statement, *arguments)

        assert await conn.fetchval("SELECT COUNT(*) FROM agent_sandbox_events") == original_count
        assert (
            await conn.fetchval(
                "SELECT event_type FROM agent_sandbox_events WHERE id = $1", event_id
            )
            != "rewritten"
        )


@pytest.mark.parametrize(("prior_head", "events_present"), _COMMITTED_HEADS)
async def test_merge_upgrades_from_each_committed_prior_head(pool, prior_head, events_present):
    """A deployment stamped on either committed branch reaches the sole head."""
    config = _db_config()
    expected_head = _migration_head()
    try:
        downgrade_to(config, _BRANCH_POINT)
        apply_migrations(config, prior_head)
        async with pool.acquire() as conn:
            versions = {
                row["version_num"]
                for row in await conn.fetch("SELECT version_num FROM alembic_version")
            }
            event_table = await conn.fetchval("SELECT to_regclass('agent_sandbox_events')")
        assert versions == {prior_head}
        assert (event_table is not None) is events_present

        apply_migrations(config)
        async with pool.acquire() as conn:
            versions = {
                row["version_num"]
                for row in await conn.fetch("SELECT version_num FROM alembic_version")
            }
            triggers = {
                row["tgname"]
                for row in await conn.fetch(
                    """
                    SELECT trigger.tgname
                    FROM pg_trigger AS trigger
                    WHERE trigger.tgrelid = 'agent_sandbox_events'::regclass
                      AND NOT trigger.tgisinternal
                    """
                )
            }
        assert versions == {expected_head}
        assert triggers == {
            "trg_agent_sandbox_events_append_only",
            "trg_agent_sandbox_events_no_truncate",
        }
    finally:
        apply_migrations(config)


async def test_merge_downgrade_preserves_historical_branch_and_reupgrades(pool):
    """Unmerging drops only the new guard; the committed trajectory survives."""
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        async with pool.acquire() as conn:
            versions = {
                row["version_num"]
                for row in await conn.fetch("SELECT version_num FROM alembic_version")
            }
            event_table = await conn.fetchval("SELECT to_regclass('agent_sandbox_events')")
            reject_function = await conn.fetchval(
                "SELECT to_regprocedure('agent_reject_sandbox_event_mutation()')"
            )
            record_function = await conn.fetchval(
                "SELECT to_regprocedure('agent_record_sandbox_lifecycle_event()')"
            )
            triggers = {
                row["tgname"]
                for row in await conn.fetch(
                    """
                    SELECT trigger.tgname
                    FROM pg_trigger AS trigger
                    WHERE trigger.tgrelid = 'agent_sandbox_events'::regclass
                      AND NOT trigger.tgisinternal
                    """
                )
            }
        assert versions == {
            "0046_admin_sandbox_trajectory",
            "0056_cloudlab_autoscaling",
        }
        assert event_table is not None
        assert reject_function is not None
        assert record_function is not None
        assert triggers == {"trg_agent_sandbox_events_append_only"}

        apply_migrations(config, _THIS)
        async with pool.acquire() as conn:
            versions = {
                row["version_num"]
                for row in await conn.fetch("SELECT version_num FROM alembic_version")
            }
            triggers = {
                row["tgname"]
                for row in await conn.fetch(
                    """
                    SELECT trigger.tgname
                    FROM pg_trigger AS trigger
                    WHERE trigger.tgrelid = 'agent_sandbox_events'::regclass
                      AND NOT trigger.tgisinternal
                    """
                )
            }
        assert versions == {_THIS}
        assert triggers == {
            "trg_agent_sandbox_events_append_only",
            "trg_agent_sandbox_events_no_truncate",
        }
    finally:
        apply_migrations(config)
