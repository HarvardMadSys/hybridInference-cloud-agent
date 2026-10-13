"""Owner profile persistence survives reloads and rejects stale concurrent edits."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to
from tests.unit.test_user_profile import data_url, picture

from cloud_agent.user_profile import ProfileConflict, ProfileView, sanitize_profile_image

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_profiles_are_persistent_owner_scoped_and_revision_checked(store, pool):
    repo = store.user_profile
    assert await repo.get_user_profile("alice") == ProfileView()
    image = sanitize_profile_image(data_url(picture()))
    args = {
        "first_name": "沐昕",
        "last_name": "田",
        "expected_revision": 0,
        "replace_image": True,
        "image_data_url": image,
    }
    results = await asyncio.gather(
        repo.save_user_profile("alice", **args),
        repo.save_user_profile("alice", **args),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ProfileConflict) for result in results) == 1
    saved = await repo.get_user_profile("alice")
    assert saved == ProfileView(first_name="沐昕", last_name="田", image_data_url=image, revision=1)
    assert await repo.get_user_profile("bob") == ProfileView()
    kept = await repo.save_user_profile(
        "alice",
        **{
            **args,
            "first_name": "New",
            "expected_revision": 1,
            "replace_image": False,
            "image_data_url": None,
        },
    )
    assert kept.image_data_url == image
    removed = await repo.save_user_profile(
        "alice", **{**args, "expected_revision": 2, "image_data_url": None}
    )
    assert removed.image_data_url is None
    assert (await repo.get_user_profile("alice")).revision == 3
    async with pool.acquire() as conn:
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_user_profiles'"
        )
        assert "email" not in {row["column_name"] for row in columns}
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_user_profiles SET first_name = repeat('x', 101) WHERE user_id = 'alice'"
            )


async def test_profile_migration_round_trip_preserves_unrelated_owner_data(store, pool):
    config = _db_config()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_user_memories (user_id, portrait) "
            "VALUES ('profile-migration-owner', '{}'::jsonb)"
        )
    try:
        for _ in range(2):
            downgrade_to(config, "0062_native_session_locator")
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT to_regclass('agent_user_profiles')") is None
            apply_migrations(config)
            assert await store.user_profile.get_user_profile("alice") == ProfileView()
            async with pool.acquire() as conn:
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM agent_user_memories WHERE user_id = 'profile-migration-owner'"
                    )
                    == 1
                )
    finally:
        apply_migrations(config)
