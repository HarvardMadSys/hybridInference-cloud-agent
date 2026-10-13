"""Permission lookup outages remain closed and retryable without revoking access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agent import entitlement
from cloud_agent.app import install_repository_access_errors
from cloud_agent.entitlement import RepoAccessUnavailable, RepoNotAllowed


def _connection(monkeypatch, *, failure: bool):
    store = SimpleNamespace(
        list_repo_grants=AsyncMock(
            return_value=[
                {"installation_id": 1},
                {"installation_id": 2},
            ]
        )
    )
    monkeypatch.setattr(
        entitlement,
        "user_access_token",
        AsyncMock(
            return_value=SimpleNamespace(value="synthetic", ciphertext="synthetic-cipher"),
        ),
    )

    async def repositories(token, installation):
        if installation == 1:
            return ["owner/accessible"]
        if failure:
            raise httpx.ConnectError("provider diagnostics must stay private")
        return []

    app = SimpleNamespace(
        cached_user_repositories=lambda *args: None,
        writable_repositories_for_user=repositories,
        remember_user_repositories=lambda *args: None,
    )
    return store, app


@pytest.mark.asyncio
async def test_partial_lookup_keeps_verified_repositories_and_defers_unknown_access(monkeypatch):
    store, app = _connection(monkeypatch, failure=True)
    assert await entitlement.repos_for_user("user", store=store, app_credentials=app, env={}) == [
        "owner/accessible",
    ]
    await entitlement.require_entitled_repo(
        "owner/accessible",
        "user",
        store=store,
        app_credentials=app,
        env={},
    )
    with pytest.raises(RepoAccessUnavailable):
        await entitlement.require_entitled_repo(
            "owner/not-verified",
            "user",
            store=store,
            app_credentials=app,
            env={},
        )


@pytest.mark.asyncio
async def test_complete_lookup_can_definitively_refuse_access(monkeypatch):
    store, app = _connection(monkeypatch, failure=False)
    with pytest.raises(RepoNotAllowed):
        await entitlement.require_entitled_repo(
            "owner/not-verified",
            "user",
            store=store,
            app_credentials=app,
            env={},
        )


@pytest.mark.asyncio
async def test_token_refresh_outage_is_retryable(monkeypatch):
    store, app = _connection(monkeypatch, failure=False)
    monkeypatch.setattr(
        entitlement,
        "user_access_token",
        AsyncMock(
            side_effect=httpx.ConnectError("private upstream diagnostics"),
        ),
    )
    with pytest.raises(RepoAccessUnavailable, match="temporarily unavailable"):
        await entitlement.require_entitled_repo(
            "owner/accessible",
            "user",
            store=store,
            app_credentials=app,
            env={},
        )


def test_repository_unavailability_is_a_bounded_uncached_retry_response():
    app = FastAPI()
    install_repository_access_errors(app)

    @app.get("/permissions")
    async def permissions():
        raise RepoAccessUnavailable("private upstream diagnostics")

    response = TestClient(app).get("/permissions")
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"]["error"]["type"] == "repo_access_unavailable"
    assert "private upstream" not in response.text
