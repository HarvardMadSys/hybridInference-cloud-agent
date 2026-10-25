"""The notification change feed is owner-bound, bounded, and never cached."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.api import catalog
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import create_app, create_mcp_app

PATH = "/v1/agent/notifications/state"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "notification-test-secret-long-enough")
    application = FastAPI()
    application.include_router(catalog.router)
    queries = AsyncMock()
    queries.notification_state.return_value = {
        "mode": "delta",
        "jobs": [
            {
                "id": "job-1",
                "thread_id": "thread-1",
                "title": "Fix the race",
                "thread_revision": 3,
                "repo": "owner/repo",
                "state": "succeeded",
                "current_attempt_id": 7,
                "turn_no": 2,
                "revision": 4,
                "cancel_requested": False,
                "finished_at": datetime(2026, 10, 22, 12, tzinfo=UTC),
                "updated_at": datetime(2026, 10, 22, 12, tzinfo=UTC),
            }
        ],
        "threads": [{"id": "thread-1", "title": "Fix the race", "revision": 3}],
        "deleted_job_ids": ["job-gone"],
        "deleted_thread_ids": ["thread-gone"],
        "observed_at": datetime(2026, 10, 22, 12, tzinfo=UTC),
        "next_cursor": None,
        "change_cursor": "checkpoint",
        "reset_required": False,
    }
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        conversation_queries=queries
    )
    application.state.queries = queries
    return application


def headers(owner: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer {identity.mint_session(user_id=owner)}"}


async def test_reads_an_owner_scoped_delta_without_caching(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.get(
            PATH, params={"cursor": "opaque", "limit": 3, "user_id": "bob"}, headers=headers()
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["jobs"][0] == {
        "id": "job-1",
        "thread_id": "thread-1",
        "title": "Fix the race",
        "thread_revision": 3,
        "repo": "owner/repo",
        "state": "succeeded",
        "current_attempt_id": 7,
        "turn_no": 2,
        "revision": 4,
        "cancel_requested": False,
        "finished_at": "2026-10-22T12:00:00+00:00",
        "updated_at": "2026-10-22T12:00:00+00:00",
    }
    assert response.json()["deleted_thread_ids"] == ["thread-gone"]
    app.state.queries.notification_state.assert_awaited_once_with(
        user_id="alice", cursor="opaque", limit=3
    )


async def test_rejects_bad_bounds_authentication_and_storage_cursors(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        assert (await client.get(PATH)).status_code == 401
        assert (await client.get(PATH, params={"limit": 201}, headers=headers())).status_code == 422
        app.state.queries.notification_state.side_effect = ValueError(
            "Invalid notification-state cursor."
        )
        invalid = await client.get(PATH, params={"cursor": "bad"}, headers=headers())

    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "Invalid notification-state cursor."


def test_notification_state_route_is_only_on_the_main_listener():
    assert PATH in create_app().openapi()["paths"]
    assert PATH not in create_mcp_app().openapi()["paths"]
