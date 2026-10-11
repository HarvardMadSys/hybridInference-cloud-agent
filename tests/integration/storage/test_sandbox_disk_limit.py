"""Disk quotas persist with the group's identity through upgrades and admin edits."""

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import (
    _reserve_shared_chat,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_disk_limit_is_immutable_and_admin_inventory_reads_the_reserved_value(pool, store):
    job, claim, sandbox = await _reserve_shared_chat(store, disk_limit_mib=16384)
    await store.host_pool.set_host_settings(changes={"sandbox_disk_mib": 32768})
    assert (await store.host_pool.host_settings())["sandbox_disk_mib"] == 32768
    reserve = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "host": sandbox["host"],
        "sandbox_id": sandbox["sandbox_id"],
        "container_name": sandbox["container_name"],
        "runtime_id": sandbox["runtime_id"],
        "runtime_version": sandbox["runtime_version"],
        "sandbox_image_digest": sandbox["sandbox_image_digest"],
        "execution_profile": sandbox["execution_profile"],
        "disk_limit_mib": 16384,
    }
    repeated = await store.sandbox.reserve_thread_sandbox(**reserve)
    assert repeated is not None and repeated["disk_limit_mib"] == 16384
    with pytest.raises(ValueError, match="immutable"):
        await store.sandbox.reserve_thread_sandbox(**{**reserve, "disk_limit_mib": 32768})
    for invalid in [-1, 16777217, True, 1.0, "16384"]:
        with pytest.raises(ValueError, match="disk limit"):
            await store.sandbox.reserve_thread_sandbox(**{**reserve, "disk_limit_mib": invalid})
    detail = await AdminSandboxQueries(pool).detail(sandbox["sandbox_id"])
    assert detail is not None and detail.sandbox.disk_limit_mib == 16384
    page = await AdminSandboxQueries(pool).list_sandboxes(cursor=None, limit=10, state="all")
    assert page.sandboxes[0].disk_limit_mib == 16384


async def test_disk_limit_upgrade_keeps_legacy_groups_unlimited_and_preserves_every_other_field(
    pool, store
):
    await _reserve_shared_chat(store)
    config = _db_config()
    downgrade_to(config, "0040_pr_merge_archive")
    try:
        async with pool.acquire() as conn:
            before = [dict(row) for row in await conn.fetch("SELECT * FROM agent_sandboxes")]
        assert before
        apply_migrations(config, "0041_sandbox_disk_limit")
        async with pool.acquire() as conn:
            after = [dict(row) for row in await conn.fetch("SELECT * FROM agent_sandboxes")]
            assert after == [{**row, "disk_limit_mib": 0} for row in before]
            for invalid in [-1, 16777217]:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute("UPDATE agent_sandboxes SET disk_limit_mib = $1", invalid)
    finally:
        apply_migrations(config)
