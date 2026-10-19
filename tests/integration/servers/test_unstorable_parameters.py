"""A parameter Postgres cannot take is refused with a 422 before any query reads it.

A NUL in a path or query parameter, or an integer too wide for a ``bigint``,
used to travel to the first query that read it and fail there with a 500.
These go through the application ``create_app`` builds, over the real store,
and only the gateway and the caller's identity stand in.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.integration.servers.test_pending_message_queue import (
    _OWNER,
    _REPO,
    _Gateway,
    pool as pool,
)

from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_storage,
    verify_admin_access,
)
from cloud_agent.app import create_app
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: Wider than a ``bigint``, whose largest value is 9223372036854775807.
WIDE = "99999999999999999999"


@pytest_asyncio.fixture
async def http(pool, monkeypatch):
    """The real application with one owner, one team, environment and chat."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "unstorable-parameters-key")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", _REPO)
    store = Storage(pool)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_gateway_client] = _Gateway
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[authenticate_agent_owner] = lambda: {
        "user_id": _OWNER,
        "role": "free",
        "authenticated": True,
    }
    app.dependency_overrides[verify_admin_access] = lambda: "admin"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        team = await store.teams.create_team(_OWNER, name="Platform", request_key="parameters")
        environment = await client.post(
            "/v1/agent/environments",
            json={"name": "Development", "repo": _REPO, "revision": 0, "setup_script": ""},
        )
        assert environment.status_code == 201, environment.text
        job = await client.post(
            "/v1/agent/jobs",
            json={"repo": _REPO, "task_prompt": "fix it", "model": "glm-5.1", "base_sha": "a" * 40},
        )
        assert job.status_code == 201, job.text
        yield client, {"team": team.id, "env": environment.json()["id"], "job": job.json()["id"]}


@pytest.mark.parametrize(
    "path",
    [
        "/admin/agent/teams?q=Plat%00form",
        "/admin/agent/teams/{team}%00",
        "/admin/agent/teams/{team}/audit-events?before=" + WIDE,
        "/v1/agent/teams/{team}%00",
        "/v1/agent/teams/{team}/audit-events?before=" + WIDE,
        "/v1/agent/environments/{env}%00",
        "/v1/agent/environments/{env}/versions?before=" + WIDE,
        "/v1/agent/jobs/{job}%00",
        "/v1/agent/jobs/{job}/events?after=" + WIDE,
        "/v1/agent/skills/" + WIDE,
    ],
)
async def test_a_parameter_postgres_cannot_take_is_refused(
    http: tuple[AsyncClient, dict[str, Any]], path: str
):
    client, ids = http

    response = await client.get(path.format(**ids))

    assert response.status_code == 422, response.text


async def test_text_that_reads_as_a_wide_integer_is_still_text(
    http: tuple[AsyncClient, dict[str, Any]],
):
    """Only a parameter the route reads as an integer is held to a ``bigint``."""
    client, _ids = http

    response = await client.get(f"/admin/agent/teams?q={WIDE}")

    assert response.status_code == 200, response.text
    assert response.json()["teams"] == []
