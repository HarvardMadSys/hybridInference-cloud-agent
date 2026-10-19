"""Read receipts use the session owner and bounded, explicit completion identities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.api import catalog
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import install_admin_no_store

PATH = "/v1/agent/conversations/read-receipts"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "receipt-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(catalog.router)
    install_admin_no_store(application, prefixes=(PATH,))
    queries = AsyncMock()
    queries.list_read_receipts.return_value = {
        "receipts": [],
        "next_cursor": "12",
        "sync_cursor": "12",
    }
    commands = AsyncMock()
    commands.merge_read_receipts.return_value = [
        {"thread_id": "thread-1", "job_id": "job-1", "turn_no": 2, "attempt_id": 7}
    ]
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        conversation_queries=queries, conversation_commands=commands
    )
    application.state.queries = queries
    application.state.commands = commands
    return application


def headers(owner="alice"):
    return {"Authorization": f"Bearer {identity.mint_session(user_id=owner)}"}


async def test_owner_is_session_bound_and_client_cannot_choose_thread_or_turn(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        read = await client.get(PATH + "?user_id=bob&cursor=10&limit=2", headers=headers())
        saved = await client.post(
            PATH,
            headers=headers(),
            json={
                "user_id": "bob",
                "receipts": [
                    {"job_id": "job-1", "attempt_id": 7, "thread_id": "foreign", "turn_no": 999}
                ],
            },
        )
    assert read.status_code == saved.status_code == 200
    assert read.headers["Cache-Control"] == saved.headers["Cache-Control"] == "no-store"
    assert read.json() == {
        "user_id": "alice",
        "receipts": [],
        "next_cursor": "12",
        "sync_cursor": "12",
    }
    assert saved.json()["receipts"] == [
        {"thread_id": "thread-1", "job_id": "job-1", "turn_no": 2, "attempt_id": 7}
    ]
    app.state.queries.list_read_receipts.assert_awaited_once_with(
        user_id="alice", after=10, limit=2
    )
    app.state.commands.merge_read_receipts.assert_awaited_once_with(
        user_id="alice", receipts=[{"job_id": "job-1", "attempt_id": 7}]
    )


@pytest.mark.parametrize(
    "receipt",
    [
        {"job_id": "job-1"},
        {"job_id": "", "attempt_id": None},
        {"job_id": "x\x00", "attempt_id": None},
        {"job_id": "x" * 129, "attempt_id": None},
        {"job_id": "job-1", "attempt_id": -1},
        {"job_id": "job-1", "attempt_id": 9007199254740992},
    ],
)
async def test_rejects_ambiguous_or_invalid_completions(app, receipt):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        result = await client.post(PATH, headers=headers(), json={"receipts": [receipt]})
    assert result.status_code == 422
    app.state.commands.merge_read_receipts.assert_not_awaited()


async def test_requires_owner_authentication_and_bounds_batches(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        assert (await client.get(PATH)).status_code == 401
        assert (await client.post(PATH, json={"receipts": []})).status_code == 401
        assert (await client.get(PATH + "?limit=201", headers=headers())).status_code == 422
        result = await client.post(
            PATH,
            headers=headers(),
            json={"receipts": [{"job_id": "job-1", "attempt_id": None}] * 201},
        )
        assert result.status_code == 422
    app.state.queries.list_read_receipts.assert_not_awaited()
    app.state.commands.merge_read_receipts.assert_not_awaited()


@pytest.mark.parametrize("cursor", ["-1", "9223372036854775808", "thread-id"])
async def test_rejects_invalid_revision_cursors(app, cursor):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        result = await client.get(PATH, params={"cursor": cursor}, headers=headers())
    assert result.status_code == 422
    app.state.queries.list_read_receipts.assert_not_awaited()
