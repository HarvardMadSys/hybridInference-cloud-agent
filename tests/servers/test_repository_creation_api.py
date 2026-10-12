"""Personal repository creation preserves owner identity and Agent entitlement."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import source_control
from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client, get_storage
from cloud_agent.github_app import (
    AppNotInstalled,
    RepositoryCreationError,
    UserAuthorizationRevoked,
)
from cloud_agent.github_user_token import UserTokenUnavailable
from cloud_agent.source_control import SourceControlCipher
from cloud_agent.source_control_runtime import (
    GitHubAppProvider,
    GitLabOAuthLoad,
    SourceControlRuntime,
)

pytestmark = pytest.mark.asyncio
_OWNER = "user-owner"
_OTHER = "user-other"


class _Gateway:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "allowed": self.allowed,
            "permissions": ["agent.use"] if self.allowed else [],
        }


class _Store:
    """Only existing source-control capabilities: creation writes no grants/jobs."""

    def __init__(self, app: _App) -> None:
        self.source_control = self
        self.accounts = {
            _OWNER: {"access_token_ciphertext": app.cipher.encrypt("ghu_owner_secret")},
            _OTHER: {"access_token_ciphertext": app.cipher.encrypt("ghu_other_secret")},
        }
        self.grants = {_OWNER: [{"installation_id": 11}], _OTHER: [{"installation_id": 22}]}
        self.reads: list[str] = []
        self.cleared: list[tuple[str, str]] = []

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        self.reads.append(user_id)
        account = self.accounts.get(user_id)
        return dict(account) if account else None

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, int]]:
        return list(self.grants.get(user_id, []))

    async def clear_github_tokens(self, *, user_id: str, access_token_ciphertext: str) -> bool:
        self.cleared.append((user_id, access_token_ciphertext))
        account = self.accounts.get(user_id)
        if account and account.get("access_token_ciphertext") == access_token_ciphertext:
            account["access_token_ciphertext"] = None
            return True
        return False


class _App:
    user_authorization_configured = True

    def __init__(self) -> None:
        self.cipher = SourceControlCipher("repository-creation-test-key")
        self.created: list[dict[str, Any]] = []
        self.forgotten: list[str] = []
        self.listed: list[tuple[str, int]] = []
        self.accessible = False
        self.covered = True
        self.installation_failure: Exception | None = None
        self.failure: Exception | None = None
        self.response: dict[str, Any] | None = None
        self.before_creation: Any = None
        self.cache = {_OWNER: ["octocat/stale"], _OTHER: ["other/stale"]}

    async def create_personal_repository(self, token: str, **details: Any) -> dict[str, Any]:
        self.created.append({"token": token, **details})
        if self.before_creation is not None:
            self.before_creation()
        if self.failure is not None:
            raise self.failure
        owner = "octocat" if token == "ghu_owner_secret" else "other"
        return (
            self.response
            if self.response is not None
            else {
                "full_name": f"{owner}/{details['name']}",
                "html_url": f"https://github.com/{owner}/{details['name']}",
                "default_branch": "trunk",
            }
        )

    def forget_user(self, user_id: str) -> None:
        self.forgotten.append(user_id)
        self.cache.pop(user_id, None)

    def cached_user_repositories(
        self, user_id: str, installation_ids: tuple[int, ...]
    ) -> list[str] | None:
        return self.cache.get(user_id)

    def remember_user_repositories(
        self, user_id: str, installation_ids: tuple[int, ...], repos: list[str]
    ) -> None:
        self.cache[user_id] = repos

    async def writable_repositories_for_user(self, token: str, installation_id: int) -> list[str]:
        self.listed.append((token, installation_id))
        return ["octocat/new-repo"] if self.accessible and installation_id == 11 else []

    async def installation_id_for(self, repo: str) -> int:
        if self.installation_failure is not None:
            raise self.installation_failure
        if not self.covered:
            raise AppNotInstalled("The App has not been given this repository")
        return 11


@pytest.fixture()
def setup(monkeypatch):
    monkeypatch.setenv(
        "AGENT_GITHUB_APP_INSTALL_URL", "https://github.com/apps/cloud-agent/installations/new"
    )
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    credentials = _App()
    store = _Store(credentials)
    app = FastAPI()
    app.include_router(source_control.router)
    app.dependency_overrides[authenticate_agent_owner] = lambda: {"user_id": _OWNER}
    app.dependency_overrides[get_gateway_client] = _Gateway
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: credentials
    return app, store, credentials


async def _post(app: FastAPI, body: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        return await client.post(
            "/v1/agent/repositories", json=body or {"name": "new-repo"}, **kwargs
        )


async def test_creation_uses_current_owners_encrypted_user_token_and_preserves_grants(setup):
    app, store, credentials = setup
    response = await _post(
        app, {"name": "new-repo", "description": "A fresh start", "private": False}
    )

    assert response.status_code == 201
    assert response.json() == {
        "repo": "octocat/new-repo",
        "web_url": "https://github.com/octocat/new-repo",
        "default_branch": "trunk",
        "accessible": False,
    }
    assert credentials.created == [
        {
            "token": "ghu_owner_secret",
            "name": "new-repo",
            "description": "A fresh start",
            "private": False,
        }
    ]
    assert set(store.reads) == {_OWNER}
    assert credentials.listed == [("ghu_owner_secret", 11)]
    assert store.grants == {_OWNER: [{"installation_id": 11}], _OTHER: [{"installation_id": 22}]}
    assert "ghu_owner_secret" not in response.text


async def test_access_is_checked_fresh_against_existing_installation_entitlement(setup):
    app, _store, credentials = setup
    credentials.accessible = True
    response = await _post(app)

    assert response.status_code == 201 and response.json()["accessible"] is True
    assert credentials.created[0]["private"] is True
    assert credentials.created[0]["description"] == ""
    assert credentials.forgotten == [_OWNER]
    assert credentials.cache[_OWNER] == ["octocat/new-repo"]
    assert credentials.cache[_OTHER] == ["other/stale"]


async def test_personal_account_without_installation_still_creates_but_cannot_run(setup):
    app, store, credentials = setup
    store.grants[_OWNER] = []
    response = await _post(app)

    assert response.status_code == 201 and response.json()["accessible"] is False
    assert credentials.listed == []
    assert store.grants[_OWNER] == []


async def test_allowlist_does_not_claim_readiness_without_app_repository_coverage(
    setup, monkeypatch
):
    app, _store, credentials = setup
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "octocat/*")
    credentials.covered = False
    response = await _post(app)

    assert response.status_code == 201 and response.json()["accessible"] is False
    assert len(credentials.created) == 1


async def test_post_creation_installation_lookup_failure_still_reports_success(setup):
    app, _store, credentials = setup
    credentials.accessible = True
    credentials.installation_failure = RuntimeError("GitHub temporarily unavailable")
    response = await _post(app)

    assert response.status_code == 201 and response.json()["accessible"] is False
    assert len(credentials.created) == 1


async def test_other_user_cannot_borrow_connected_owners_token(setup):
    app, store, credentials = setup
    app.dependency_overrides[authenticate_agent_owner] = lambda: {"user_id": _OTHER}
    store.accounts.pop(_OTHER)
    response = await _post(app)

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "github_reconnect_required"
    assert credentials.created == [] and store.reads == [_OTHER]


async def test_each_connected_user_creates_in_their_own_account(setup):
    app, store, credentials = setup
    app.dependency_overrides[authenticate_agent_owner] = lambda: {"user_id": _OTHER}
    response = await _post(app)

    assert response.status_code == 201 and response.json()["repo"] == "other/new-repo"
    assert credentials.created[0]["token"] == "ghu_other_secret"
    assert set(store.reads) == {_OTHER}
    assert credentials.listed == [("ghu_other_secret", 22)]


@pytest.mark.parametrize(
    "headers,expected", [({}, 401), ({"Authorization": "Bearer ajt.fake"}, 403)]
)
async def test_session_authentication_required_and_worker_tokens_refused(setup, headers, expected):
    app, store, credentials = setup
    app.dependency_overrides.pop(authenticate_agent_owner)
    response = await _post(app, headers=headers)
    assert response.status_code == expected
    assert credentials.created == [] and store.reads == []


async def test_gateway_admission_cannot_be_bypassed(setup):
    app, store, credentials = setup
    app.dependency_overrides[get_gateway_client] = lambda: _Gateway(allowed=False)
    response = await _post(app)
    assert response.status_code == 403
    assert credentials.created == [] and store.reads == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "a" * 101},
        {"name": "owner/repo"},
        {"name": " new-repo"},
        {"name": "new-repo", "owner": "someone-else"},
        {"name": "new-repo", "private": "false"},
        {"name": "new-repo", "description": "x" * 351},
        {"name": "new-repo", "description": "unsafe\x00"},
    ],
)
async def test_invalid_payloads_never_reach_github(setup, body):
    app, _store, credentials = setup
    response = await _post(app, body)
    assert response.status_code == 422 and credentials.created == []


@pytest.mark.parametrize(
    "status,error_type,phrase",
    [
        ("unavailable", "source_control_unavailable", "temporarily"),
        ("unconfigured", "github_not_configured", "not configured"),
    ],
)
async def test_provider_unavailable_and_unconfigured_are_actionable(
    setup, status, error_type, phrase
):
    app, store, credentials = setup
    if status == "unavailable":
        # Exercise the real strict dependency: a configured App that could not
        # load must fail before reading this user's token or invoking the POST.
        app.dependency_overrides.pop(get_agent_app_credentials)
        app.state.source_control = SourceControlRuntime(
            github=GitHubAppProvider({"AGENT_GITHUB_APP_ID": "1"}),
            gitlab=GitLabOAuthLoad({}),
        )
    else:
        app.dependency_overrides[get_agent_app_credentials] = lambda: None
    response = await _post(app)
    assert response.status_code == 503
    error = response.json()["detail"]["error"]
    assert error["type"] == error_type and phrase in error["message"]
    assert credentials.created == []
    assert store.reads == []
    if status == "unavailable":
        assert response.headers["Retry-After"] == "30"


@pytest.mark.parametrize("replace", [False, True])
async def test_revoked_token_is_cleared_for_only_the_current_owner_and_generation(setup, replace):
    app, store, credentials = setup
    old_ciphertext = store.accounts[_OWNER]["access_token_ciphertext"]
    if replace:
        credentials.before_creation = lambda: store.accounts[_OWNER].update(
            access_token_ciphertext=credentials.cipher.encrypt("ghu_new")
        )
    credentials.failure = UserAuthorizationRevoked("Never echo ghu_owner_secret")
    response = await _post(app)

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "github_reconnect_required"
    assert store.cleared == [(_OWNER, old_ciphertext)]
    assert bool(store.accounts[_OWNER]["access_token_ciphertext"]) is replace
    assert store.accounts[_OTHER]["access_token_ciphertext"] is not None
    assert "ghu_owner_secret" not in response.text


@pytest.mark.parametrize(
    "reason,status,error_type,phrase",
    [
        ("forbidden", 403, "repository_create_forbidden", "Repository creation write"),
        ("already_exists", 409, "repository_already_exists", "already exists"),
        ("invalid", 422, "repository_create_invalid", "rejected"),
        ("unavailable", 503, "repository_creation_unavailable", "limiting"),
        ("uncertain", 503, "repository_creation_unavailable", "Check your GitHub repositories"),
    ],
)
async def test_creation_failures_have_safe_actionable_messages_and_are_sent_once(
    setup, reason, status, error_type, phrase
):
    app, store, credentials = setup
    credentials.failure = RepositoryCreationError(reason)
    response = await _post(app)
    assert response.status_code == status
    error = response.json()["detail"]["error"]
    assert error["type"] == error_type and phrase in error["message"]
    assert len(credentials.created) == 1 and store.cleared == []
    assert "ghu_owner_secret" not in response.text


async def test_invalid_created_metadata_preserves_uncertain_outcome_guidance(setup):
    app, _store, credentials = setup
    credentials.response = {
        "full_name": "invalid",
        "html_url": "javascript:unsafe",
        "default_branch": "main",
    }
    response = await _post(app)
    assert response.status_code == 503
    assert "Check your GitHub repositories" in response.json()["detail"]["error"]["message"]
    assert len(credentials.created) == 1


async def test_token_renewal_busy_is_reported_before_any_creation(setup, monkeypatch):
    app, _store, credentials = setup

    async def renewing(*args, **kwargs):
        raise UserTokenUnavailable("ghu_owner_secret")

    monkeypatch.setattr(source_control, "user_access_token", renewing)
    response = await _post(app)
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "github_unavailable"
    assert credentials.created == [] and "ghu_owner_secret" not in response.text


async def test_corrupt_token_fails_closed_before_any_creation(setup):
    app, store, credentials = setup
    store.accounts[_OWNER]["access_token_ciphertext"] = "bad-ciphertext"
    response = await _post(app)
    assert response.status_code == 503 and credentials.created == []
    assert "bad-ciphertext" not in response.text
