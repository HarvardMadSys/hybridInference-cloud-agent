"""Grouped sidebar invalidation is additive and scoped to the authenticated owner."""

from unittest.mock import AsyncMock

import pytest
from tests.servers.test_agent_jobs_api import (
    client as client,
    store as store,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("token", [None, "01" * 64])
async def test_project_refresh_token_is_optional_and_forwarded(client, store, monkeypatch, token):
    row = {
        "repo": "owner/name",
        "task_count": 2,
        "active_count": 1,
        "last_activity_at": None,
    }
    if token is not None:
        row["refresh_token"] = token
        row["membership_token"] = token
    read = AsyncMock(return_value=[row])
    monkeypatch.setattr(store, "list_projects", read)
    response = await client.get("/v1/agent/projects")
    assert response.status_code == 200
    assert response.json()["projects"][0]["refresh_token"] == token
    assert response.json()["projects"][0]["membership_token"] == token
    read.assert_awaited_once_with(user_id="user-owner", archived=False)
