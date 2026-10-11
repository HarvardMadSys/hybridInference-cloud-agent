"""GitLab route wiring against the real OAuth client and storage assembly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent.api import source_control
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_agent_gitlab_oauth, get_storage
from cloud_agent.ports import McpConnectionRepository, SourceControlRepository
from cloud_agent.source_control import GitLabOAuthClient, GitLabOAuthConfig, SourceControlCipher
from cloud_agent.storage.assembly import Storage

if TYPE_CHECKING:
    from collections.abc import Callable

_OWNER = "user-owner"


def _app(store: Storage, gitlab: GitLabOAuthClient) -> FastAPI:
    app = FastAPI()
    app.include_router(source_control.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": _OWNER}
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_gitlab_oauth] = lambda: gitlab
    return app


@pytest.fixture
def store() -> Storage:
    # Use the real assembly: flat capability fakes also expose repository
    # methods at the top level and hide a miswired client argument.
    assembly = Storage(Mock())
    repository = AsyncMock(spec=SourceControlRepository)
    repository.list_repo_grants.return_value = []
    repository.get_github_account.return_value = None
    repository.get_gitlab_connection.return_value = None
    assembly.source_control = repository
    assembly.mcp = AsyncMock(spec=McpConnectionRepository)
    assembly.mcp.list_mcp_connections.return_value = []
    return assembly


def _gitlab(handler: Callable[[httpx.Request], httpx.Response]) -> GitLabOAuthClient:
    return GitLabOAuthClient(
        GitLabOAuthConfig("client-id", "client-secret", "https://example.com/callback"),
        cipher=SourceControlCipher("server-secret"),
        transport=httpx.MockTransport(handler),
    )


def _connection(gitlab: GitLabOAuthClient, *, expired: bool) -> dict[str, Any]:
    return {
        "id": 7,
        "user_id": _OWNER,
        "username": "octo",
        "web_url": "https://gitlab.com/octo",
        "access_token_ciphertext": gitlab.cipher.encrypt("access-one"),
        "refresh_token_ciphertext": gitlab.cipher.encrypt("refresh-one"),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1),
    }


@pytest.mark.parametrize("connection_state", ["disconnected", "live", "expired"])
@pytest.mark.asyncio
async def test_integrations_gitlab_uses_source_control_repository(
    store: Storage, connection_state: str
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/oauth/token":
            assert parse_qs(request.content.decode())["refresh_token"] == ["refresh-one"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-two",
                    "refresh_token": "refresh-two",
                    "expires_in": 7200,
                    "created_at": datetime.now(timezone.utc).timestamp(),
                },
            )
        assert request.url.path == "/api/v4/projects"
        token = "access-two" if connection_state == "expired" else "access-one"
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 9,
                    "path_with_namespace": "octo/project",
                    "web_url": "https://gitlab.com/octo/project",
                }
            ],
        )

    gitlab = _gitlab(handler)
    if connection_state != "disconnected":
        store.source_control.get_gitlab_connection.return_value = _connection(
            gitlab, expired=connection_state == "expired"
        )
    app = _app(store, gitlab)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/agent/integrations")

    assert response.status_code == 200
    provider = response.json()["providers"][1]
    assert provider["configured"] is True
    assert provider["connected"] is (connection_state != "disconnected")
    assert provider["error"] is None
    query = parse_qs(urlparse(provider["connect_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    store.source_control.create_oauth_state.assert_awaited_once()
    state = store.source_control.create_oauth_state.await_args.kwargs
    assert state["user_id"] == _OWNER
    assert state["provider"] == "gitlab"
    assert gitlab.cipher.decrypt(state["code_verifier_ciphertext"])
    if connection_state == "disconnected":
        assert provider["repositories"] == []
        assert requests == []
    else:
        assert provider["repositories"][0]["name"] == "octo/project"
        assert "/api/v4/projects" in requests
    if connection_state == "expired":
        update = store.source_control.update_gitlab_tokens.await_args.kwargs
        assert update["user_id"] == _OWNER
        assert update["connection_id"] == 7
        assert gitlab.cipher.decrypt(update["access_token_ciphertext"]) == "access-two"
        assert gitlab.cipher.decrypt(update["refresh_token_ciphertext"]) == "refresh-two"
    else:
        store.source_control.update_gitlab_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_gitlab_refreshes_and_revokes_with_source_control_repository(
    store: Storage,
) -> None:
    revoked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if request.url.path == "/oauth/token":
            assert form["refresh_token"] == ["refresh-one"]
            return httpx.Response(
                200,
                json={"access_token": "access-two", "refresh_token": "refresh-two"},
            )
        assert request.url.path == "/oauth/revoke"
        revoked.extend(form["token"])
        return httpx.Response(200, json={})

    gitlab = _gitlab(handler)
    store.source_control.get_gitlab_connection.side_effect = [
        _connection(gitlab, expired=True),
        None,
    ]
    app = _app(store, gitlab)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete("/v1/agent/integrations/gitlab/connections/7")

    assert response.status_code == 200
    assert revoked == ["access-two"]
    store.source_control.update_gitlab_tokens.assert_awaited_once()
    store.source_control.delete_gitlab_connection.assert_awaited_once_with(
        user_id=_OWNER, connection_id=7
    )
    assert response.json()["providers"][1]["connected"] is False
