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
from cloud_agent.github_app import AppConfig, GitHubAppCredentials

# Listing a user's repositories speaks as the user, so no App key is used.
_APP = AppConfig(app_id="1", private_key="unused", api_base="https://api.test")


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


def _listed_connection(monkeypatch):
    """A user connected to one installation, whose listing GitHub answers."""
    monkeypatch.setattr(
        entitlement,
        "user_access_token",
        AsyncMock(
            return_value=SimpleNamespace(value="synthetic", ciphertext="synthetic-cipher"),
        ),
    )
    return SimpleNamespace(list_repo_grants=AsyncMock(return_value=[{"installation_id": 77}]))


@pytest.mark.asyncio
@pytest.mark.parametrize("limited_page", ["1", "2"])
async def test_a_rate_limited_listing_is_unavailable_and_not_remembered(monkeypatch, limited_page):
    """A rate limit, which GitHub may answer with a 403, is no answer about access.

    Read as the installation's refusal, it was an empty answer, settled and kept
    for a minute; a limit on a later page also discarded the pages before it.
    """
    store = _listed_connection(monkeypatch)
    first_page = [{"full_name": f"owner/r{n}", "permissions": {"push": True}} for n in range(100)]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == limited_page:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
                json={"message": "API rate limit exceeded"},
            )
        return httpx.Response(200, json={"repositories": first_page})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        app = GitHubAppCredentials(_APP, client=client)
        with pytest.raises(RepoAccessUnavailable):
            await entitlement.require_entitled_repo(
                "owner/r0", "user", store=store, app_credentials=app, env={}
            )
    assert app.cached_user_repositories("user", (77,)) is None


@pytest.mark.asyncio
async def test_a_plain_refusal_of_the_listing_is_still_no_access(monkeypatch):
    store = _listed_connection(monkeypatch)
    refused = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: refused)) as client:
        app = GitHubAppCredentials(_APP, client=client)
        with pytest.raises(RepoNotAllowed):
            await entitlement.require_entitled_repo(
                "owner/r0", "user", store=store, app_credentials=app, env={}
            )
    assert app.cached_user_repositories("user", (77,)) == []


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
