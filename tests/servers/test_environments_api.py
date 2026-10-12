"""Environment validation never reflects credentials or bypasses owner scope."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import environments
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.environments import EnvironmentNotFound

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    repository = SimpleNamespace(
        get_environment=AsyncMock(return_value={"id": "env", "revision": 1, "variables": []}),
        get_global_environment=AsyncMock(return_value={"id": None, "revision": 0, "variables": []}),
    )
    app = FastAPI()
    app.include_router(environments.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(environments=repository)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "alice"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, repository


async def test_import_preview_exposes_only_names_and_no_store(client):
    http, repo = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={
            "environment_id": "env",
            "revision": 1,
            "text": 'TOKEN="demo-private-token"\nREGION=east',
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "demo-private-token" not in response.text
    assert "east" not in response.text
    assert [v["name"] for v in response.json()["variables"]] == ["TOKEN", "REGION"]
    repo.get_environment.assert_awaited_once_with("alice", "env")


@pytest.mark.parametrize(
    "text",
    [
        "TOKEN=demo-private-token\nOPENAI_API_KEY=secret",
        "TOKEN=demo-private-token\nINVALID",
        "TOKEN=demo-private-token\nTOKEN=second",
    ],
)
async def test_invalid_import_is_atomic_and_errors_do_not_echo_values(client, text):
    http, _ = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={"environment_id": "env", "revision": 1, "text": text},
    )
    assert response.status_code == 422
    assert "demo-private-token" not in response.text
    assert "variables" not in response.json()


async def test_cross_owner_environment_is_not_visible(client):
    http, repo = client
    repo.get_environment.side_effect = EnvironmentNotFound("Environment not found")
    response = await http.get("/v1/agent/environments/foreign")
    assert response.status_code == 404
    repo.get_environment.assert_awaited_once_with("alice", "foreign")


async def test_pydantic_extra_value_errors_do_not_echo_submitted_secret(client):
    http, _ = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={
            "environment_id": "env",
            "revision": 1,
            "text": "TOKEN=value",
            "unexpected": "demo-private-token",
        },
    )
    assert response.status_code == 422
    assert "demo-private-token" not in response.text


async def test_withdrawn_test_owner_gets_no_credentials_or_container(monkeypatch):
    from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client

    run = {
        "id": "test-run",
        "user_id": "alice",
        "repo": "owner/repo",
        "binding_id": "bound",
        "lease_generation": 2,
    }
    repository = SimpleNamespace(
        claim_environment_run=AsyncMock(return_value=run),
        update_environment_run=AsyncMock(return_value=True),
        get_environment_binding=AsyncMock(),
    )
    gateway = SimpleNamespace(
        agent_access=AsyncMock(
            return_value={"user_id": "alice", "allowed": False, "permissions": []}
        )
    )
    credentials = SimpleNamespace(token_for=AsyncMock())
    app = FastAPI()
    app.include_router(environments.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(environments=repository)
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    app.dependency_overrides[get_agent_app_credentials] = lambda: credentials
    app.dependency_overrides[environments.verify_claiming_host] = lambda: {
        "host": "runner",
        "credential_hash": "hash",
    }
    entitlement = AsyncMock()
    monkeypatch.setattr(environments, "_entitled", entitlement)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(
            "/v1/agent/worker/environment-runs/claim",
            json={"capabilities": ["environment-v1"], "attestation": {}},
        )
    assert response.status_code == 503
    entitlement.assert_not_awaited()
    credentials.token_for.assert_not_awaited()
    repository.get_environment_binding.assert_not_awaited()
    repository.update_environment_run.assert_awaited_once_with(
        "test-run",
        "runner",
        2,
        {
            "state": "failed",
            "detail": "Environment credentials or repository access are unavailable",
            "cleanup_complete": True,
        },
    )
