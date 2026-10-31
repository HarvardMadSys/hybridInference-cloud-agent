"""A new task starts on the model and connection of the owner's newest turn, and no one else's."""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store

from cloud_agent.model_preference import ModelPreferenceView

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _last_job_model(store, user_id: str, model: str, connection_id: str | None) -> None:
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
        model_connection_id=connection_id,
    )


async def test_the_last_model_comes_from_the_owners_newest_turn(store):
    repo = store.model_preferences
    assert await repo.get_model_preference("carol") == ModelPreferenceView()

    await _last_job_model(store, "carol", "qwen-next", "mconn_" + "c" * 32)
    remembered = await repo.get_model_preference("carol")
    assert remembered.last_model == "qwen-next"
    assert remembered.last_model_connection_id == "mconn_" + "c" * 32

    # A newer turn moves it; someone else's turn never supplies it.
    await _last_job_model(store, "carol", "glm-5.1", "mconn_" + "d" * 32)
    await _last_job_model(store, "dave", "other-model", "mconn_" + "e" * 32)
    latest = await repo.get_model_preference("carol")
    assert (latest.last_model, latest.last_model_connection_id) == (
        "glm-5.1",
        "mconn_" + "d" * 32,
    )
    assert await repo.get_model_preference("erin") == ModelPreferenceView()
