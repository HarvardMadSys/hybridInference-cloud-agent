"""Persist native Messages connections and keep Claude follow-ups on their source."""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import pytest
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _create_job,
    pool,
    store,
)
from tests.support.follow_ups import append_follow_up

from cloud_agent.application.model_connections import new_connection_id

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "protocols",
    [
        ["anthropic_messages"],
        ["chat_completions", "responses", "anthropic_messages"],
    ],
)
async def test_messages_connection_survives_storage_and_claude_follow_up(pool, store, protocols):
    source = await store.model_connections.create_model_connection(
        user_id="user-1",
        values={
            "id": new_connection_id(),
            "name": "Native Messages",
            "base_url": "https://provider.example/anthropic/v1",
            "protocols": protocols,
            "models": ["fixture-claude"],
            "api_key_ciphertext": "fixture-encrypted-key",
        },
    )
    stored = await store.model_connections.get_model_connection(
        user_id="user-1", connection_id=source["id"]
    )
    assert stored["protocols"] == protocols
    assert (
        await store.model_connections.get_model_connection(
            user_id="user-2", connection_id=source["id"]
        )
        is None
    )
    first = await _create_job(
        store,
        runtime="claude-code",
        model="fixture-claude",
        model_connection_id=source["id"],
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'cancelled' WHERE id = $1", first["id"])
    follow_up = await append_follow_up(
        pool, parent_job_id=first["id"], user_id="user-1", prompt="continue"
    )
    assert follow_up["runtime"] == "claude-code"
    assert follow_up["model"] == "fixture-claude"
    assert follow_up["model_connection_id"] == source["id"]
    assert follow_up["reasoning_effort"] is None
