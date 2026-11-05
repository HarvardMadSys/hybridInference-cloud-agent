"""Owner scoping and the agent-reported usage of one job, over the real API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import OneFake
from tests.support.usage import END, START

from cloud_agent.api import usage
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app, create_mcp_app

VERSION = "00000000000000000042.999999"


@pytest.fixture
def store():
    events = [
        {
            "job_id": "ajob_1",
            "attempt_id": attempt,
            "seq": 1,
            "event_type": "message",
            "created_at": END,
            "payload": {
                "has_text": True,
                "usage_id": "same-id",
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 80},
            },
        }
        for attempt in (10, 11)
    ]
    fake = SimpleNamespace(
        get_job=AsyncMock(return_value={"id": "ajob_1", "user_id": "owner"}),
        job_usage_inputs=AsyncMock(
            return_value={
                "job_ids": ["ajob_1"],
                "attempts": [{"job_id": "ajob_1", "started_at": START}],
                "events": events,
            }
        ),
    )

    async def job_usage_reads(*, job_id: str, user_id: str) -> dict | None:
        # The one-snapshot read, made of the rows each test sets above — and
        # scoped the way the real snapshot read is: None for a job that is
        # not this caller's own undeleted one. The stand-in's jobs carry no
        # deleted_at, so the deleted-conversation case stays pinned by the
        # real SQL's predicate, not here.
        if job_id != "ajob_1" or user_id != "owner" or fake.get_job.return_value is None:
            return None
        job = fake.get_job.return_value
        if job.get("deleted_at") is not None:
            return None
        return {
            "job": job,
            "inputs": fake.job_usage_inputs.return_value,
            "version": VERSION,
        }

    fake.job_usage_reads = AsyncMock(side_effect=job_usage_reads)
    return OneFake(fake)


@pytest.fixture
def app(store):
    app = FastAPI()
    app.include_router(usage.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "owner"}
    return app


@pytest.mark.asyncio
async def test_usage_is_what_the_agent_reported_and_nothing_else(app, store):
    """Each attempt's report counts once; there is no cost, ledger or conversation total."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "runtime": {
            "source": "runtime",
            "input_tokens": 200,
            "output_tokens": 20,
            "cache_read_tokens": 160,
            # Not reported, so unknown rather than zero.
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "normalized_input_tokens": None,
        },
        "external_provider": False,
        "version": VERSION,
    }
    store.job_usage_reads.assert_awaited_once_with(job_id="ajob_1", user_id="owner")


@pytest.mark.asyncio
async def test_a_usage_read_answers_from_the_scoped_snapshot_alone(app, store):
    """GET /usage fences inside the owner-scoped snapshot read, with no whole-job fetch."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 200
    store.get_job.assert_not_awaited()
    store.job_usage_reads.assert_awaited_once_with(job_id="ajob_1", user_id="owner")


@pytest.mark.asyncio
async def test_another_owner_cannot_read_usage(app, store):
    store.get_job.return_value = {"id": "ajob_other", "user_id": "other"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_other/usage")
    assert response.status_code == 404
    # The read itself is owner-scoped: it refuses by answering None.
    store.job_usage_reads.assert_awaited_once_with(job_id="ajob_other", user_id="owner")


@pytest.mark.asyncio
async def test_a_deleted_turns_usage_reads_nothing(app, store):
    store.get_job.return_value = {
        "id": "ajob_1",
        "user_id": "owner",
        "deleted_at": datetime.now(UTC),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_job_deleted_between_the_check_and_the_read_is_not_found(app, store):
    store.job_usage_reads.side_effect = None
    store.job_usage_reads.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["cancelled", "failed", "succeeded", "queued"])
async def test_a_job_whose_agent_reported_nothing_has_no_counts(app, store, state):
    """No report is unknown in every state: never zero, never a promise of one."""
    store.get_job.return_value = {"id": "ajob_1", "user_id": "owner", "state": state}
    store.job_usage_inputs.return_value = {"job_ids": ["ajob_1"], "attempts": [], "events": []}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == {
        "source": "runtime",
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "normalized_input_tokens": None,
    }
    # The read answers from the owner-scoped snapshot alone.
    store.get_job.assert_not_awaited()
    store.job_usage_reads.assert_awaited_once_with(job_id="ajob_1", user_id="owner")


@pytest.mark.asyncio
async def test_codex_reasoning_is_shown_beside_output_never_added(app, store):
    store.get_job.return_value = {"id": "ajob_1", "user_id": "owner", "runtime": "codex"}
    store.job_usage_inputs.return_value["events"] = [
        {
            "job_id": "ajob_1",
            "attempt_id": 10,
            "seq": 1,
            "event_type": "usage",
            "created_at": END,
            "payload": {
                "input_tokens": 400,
                "output_tokens": 30,
                "cache_read_tokens": 250,
                "cache_write_tokens": 0,
                "reasoning_output_tokens": 12,
                "usage_id": "codex:thread-a:430",
            },
        }
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    runtime = response.json()["runtime"]
    assert (runtime["output_tokens"], runtime["reasoning_tokens"]) == (30, 12)
    assert runtime["normalized_input_tokens"] == 400


def test_usage_routes_are_available_only_on_the_control_plane():
    public_paths = create_app().openapi()["paths"]
    relay_paths = create_mcp_app().openapi()["paths"]
    path = "/v1/agent/jobs/{job_id}/usage"
    assert path in public_paths
    assert path not in relay_paths
    assert "/v1/agent/jobs/{job_id}/usage/refresh" not in public_paths


@pytest.mark.asyncio
async def test_claude_fallback_normalizes_cache_instead_of_changing_input_definition(app, store):
    store.get_job.return_value = {"id": "ajob_1", "user_id": "owner", "runtime": "claude-code"}
    for event in store.job_usage_inputs.return_value["events"]:
        event["payload"]["usage"]["cache_creation_input_tokens"] = 20
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    runtime = response.json()["runtime"]
    assert runtime["input_tokens"] == 200
    assert runtime["normalized_input_tokens"] == 400
