"""Claim-time personalization stays owner-scoped and never blocks execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.servers.test_agent_jobs_api import (
    _OWNER,
    _api_key_secret as _api_key_secret,
    _create_job,
    client as client,
    store as store,
)

from cloud_agent.api import worker


@pytest.mark.parametrize("enabled", [True, False])
async def test_claim_uses_only_enabled_preferences_from_the_job_owner(client, store, enabled):
    job_id = await _create_job(client)
    refresh = AsyncMock(
        return_value={
            "enabled": enabled,
            "memories": [
                {
                    "kind": "preference",
                    "content": "I prefer concise summaries.",
                    "source_repo": "owner/name",
                },
                {"kind": "task", "content": "A prior unrelated task"},
                {
                    "kind": "preference",
                    "content": "I prefer verbose database migration reports.",
                    "source_repo": "another/project",
                },
            ],
        }
    )
    store.user_memory = SimpleNamespace(refresh=refresh)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == job_id
    prompt = claim.json()["task_prompt"]
    assert ("I prefer concise summaries." in prompt) is enabled
    assert "prior unrelated task" not in prompt
    assert "verbose database" not in prompt
    assert prompt.endswith("fix it")
    assert store.jobs[job_id]["task_prompt"] == "fix it"
    refresh.assert_awaited_once_with(user_id=_OWNER)


async def test_claim_still_starts_when_memory_temporarily_fails(client, store):
    job_id = await _create_job(client)
    store.user_memory = SimpleNamespace(refresh=AsyncMock(side_effect=RuntimeError("unavailable")))
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == job_id
    assert claim.json()["task_prompt"] == "fix it"


async def test_slow_memory_is_cancelled_before_it_can_strand_the_claim(client, store, monkeypatch):
    job_id = await _create_job(client)
    cancelled = asyncio.Event()

    async def refresh(**_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(worker, "_USER_MEMORY_TIMEOUT_SECONDS", 0.01)
    store.user_memory = SimpleNamespace(refresh=refresh)
    claim = await asyncio.wait_for(
        client.post("/v1/agent/worker/claim", json={"worker_id": "w1"}), timeout=1
    )
    assert cancelled.is_set()
    assert claim.status_code == 200, claim.text
    assert claim.json()["job_id"] == job_id
    assert claim.json()["task_prompt"] == "fix it"
