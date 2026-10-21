"""A pinned model survives a reload, stays owner-scoped, and follows the last turn."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.model_preference import ModelPreferenceView

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _last_job_model(store, user_id: str, model: str) -> None:
    """Give an owner one turn through the real write path.

    Through ``create_job`` and not a hand-written INSERT: the repository reads
    the newest ``agent_jobs`` row, and a row assembled to fit this test would
    not prove the columns a created turn actually carries.
    """
    await store.conversation_commands.create_job(
        user_id=user_id,
        repo="example-org/example-repo",
        task_prompt="fix the flaky test",
        runtime="claude-code",
        model=model,
    )


async def test_the_pin_is_persistent_and_owner_scoped(store):
    repo = store.model_preferences
    assert await repo.get_model_preference("alice") == ModelPreferenceView()

    pinned = await repo.set_default_model("alice", "glm-5.1")
    assert pinned.default_model == "glm-5.1"
    # A second read is the reload: the pin came back from the row, not memory.
    assert (await repo.get_model_preference("alice")).default_model == "glm-5.1"
    # Another owner is untouched by the write.
    assert await repo.get_model_preference("bob") == ModelPreferenceView()

    cleared = await repo.set_default_model("alice", None)
    assert cleared.default_model is None
    assert (await repo.get_model_preference("alice")).default_model is None


async def test_the_last_model_comes_from_the_owners_newest_turn(store):
    repo = store.model_preferences
    assert await repo.get_model_preference("carol") == ModelPreferenceView()

    await _last_job_model(store, "carol", "qwen-next")
    remembered = await repo.get_model_preference("carol")
    assert remembered.last_model == "qwen-next"
    # With no pin, the remembered model is what a new task starts on.
    assert remembered.effective_model == "qwen-next"

    # A pin outranks it but does not overwrite it.
    pinned = await repo.set_default_model("carol", "glm-5.1")
    assert pinned.last_model == "qwen-next"
    assert pinned.effective_model == "glm-5.1"

    # Someone else's turn never supplies this owner's remembered model.
    await _last_job_model(store, "dave", "other-model")
    assert (await repo.get_model_preference("carol")).last_model == "qwen-next"


async def test_an_empty_pin_and_a_control_character_are_handled_by_the_schema(store, pool):
    repo = store.model_preferences
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO agent_user_model_preferences (user_id, default_model) "
                "VALUES ('erin', '')"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO agent_user_model_preferences (user_id, default_model) "
                "VALUES ('erin', 'bad' || chr(10) || 'model')"
            )
    # The refused writes left nothing behind.
    assert await repo.get_model_preference("erin") == ModelPreferenceView()


async def test_the_migration_round_trip_preserves_unrelated_owner_data(store, pool):
    config = _db_config()
    await store.model_preferences.set_default_model("round-trip", "glm-5.1")
    try:
        for _ in range(2):
            downgrade_to(config, "0089_superseded_removals")
            async with pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT to_regclass('agent_user_model_preferences')")
                    is None
                )
            apply_migrations(config)
            assert (
                await store.model_preferences.get_model_preference("round-trip")
            ).default_model is None
    finally:
        apply_migrations(config)
