"""The home page's insights are derived for their owner on every read."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from tests.support.owners import SESSIONS, use_session_secret

from cloud_agent.api import work_insights
from cloud_agent.api.deps import get_owner_directory, get_storage

PATH = "/v1/agent/insights"


@pytest.fixture()
def app(monkeypatch):
    use_session_secret(monkeypatch)
    application = FastAPI()
    application.include_router(work_insights.router)
    repository = AsyncMock()
    repository.recent_requests.return_value = ["Fix the login crash in the React frontend"]
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_owner_directory] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        local_accounts=SESSIONS, work_insights=repository
    )
    application.state.repository = repository
    return application


async def test_insights_are_the_owners_and_never_cached(app):
    session = SESSIONS.sign_in("owner-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as http:
        read = await http.get(PATH, headers={"Authorization": f"Bearer {session}"})
        anonymous = await http.get(PATH)
    assert read.status_code == 200 and anonymous.status_code == 401
    assert read.headers["cache-control"] == "no-store"
    body = read.json()
    assert body["focus_areas"][:2] == ["Frontend and UI", "Reliability and debugging"]
    assert [item["id"] for item in body["suggestions"]] == [
        "acceptance-checks",
        "verification",
        "reproduction",
    ]
    assert body["avatar"]["data_url"].startswith("data:image/svg+xml")
    app.state.repository.recent_requests.assert_awaited_once_with("owner-1", 50)
