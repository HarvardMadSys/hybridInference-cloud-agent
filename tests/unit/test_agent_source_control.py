"""Security and protocol tests for source-control OAuth connections."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import cloud_agent.source_control as source_control
from cloud_agent.source_control import (
    GitLabOAuthClient,
    GitLabOAuthConfig,
    OAuthStateError,
    SourceControlCipher,
    SourceControlError,
    consume_oauth_state,
    github_authorization_url,
    issue_oauth_state,
)


class MemoryStore:
    """Small faithful stand-in for the store's atomic ownership checks."""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.token_update: dict | None = None
        self.gitlab_refresh_claim: dict | None = None
        self.row: dict | None = None
        self.generation = 0
        self.lease_held = False
        self.reject_write = False
        self.writes: list[dict] = []
        self.after_rejected_write = None
        self.after_failed_claim = None
        self.after_claim = None
        self._explicit_claim = False
        self.claimed_lease_seconds: int | None = None

    @asynccontextmanager
    async def serialize_gitlab_account(self, *, user_id: str):
        assert user_id == "user-a"
        yield self

    async def create_oauth_state(self, **row) -> None:
        self.states[row["state_hash"]] = {**row, "consumed": False}

    async def consume_oauth_state(self, *, state_hash, user_id, provider):
        row = self.states.get(state_hash)
        if (
            row is None
            or row["user_id"] != user_id
            or row["provider"] != provider
            or row["consumed"]
            or row["expires_at"] <= datetime.now(timezone.utc)
        ):
            return None
        row["consumed"] = True
        return {"code_verifier_ciphertext": row["code_verifier_ciphertext"]}

    async def claim_gitlab_token_refresh(
        self, *, connection_id: int, user_id: str, lease_seconds: int, margin_seconds: int
    ) -> dict | None:
        assert margin_seconds == 300
        self.claimed_lease_seconds = lease_seconds
        if self.gitlab_refresh_claim is not None:
            claim, self.gitlab_refresh_claim = self.gitlab_refresh_claim, None
            self._explicit_claim = True
            return claim
        if (
            self.row is None
            or self.lease_held
            or self.row.get("id") != connection_id
            or self.row.get("user_id") != user_id
        ):
            if self.lease_held and self.after_failed_claim is not None:
                self.after_failed_claim()
            return None
        self.lease_held = True
        self.generation += 1
        if self.after_claim is not None:
            self.after_claim()
        return {
            "refresh_generation": self.generation,
            "refresh_token_ciphertext": self.row["refresh_token_ciphertext"],
        }

    async def update_gitlab_tokens(self, **values) -> bool:
        self.writes.append(dict(values))
        if self._explicit_claim:
            self._explicit_claim = False
            self.token_update = values
            return True
        accepted = (
            not self.reject_write
            and self.row is not None
            and values.get("refresh_generation") == self.generation
            and values.get("user_id") == self.row.get("user_id")
            and values.get("connection_id") == self.row.get("id")
        )
        if not accepted:
            if self.after_rejected_write is not None:
                self.after_rejected_write()
                self.lease_held = False
            return False
        self.token_update = values
        self.lease_held = False
        self.row = {
            **self.row,
            "access_token_ciphertext": values["access_token_ciphertext"],
            "refresh_token_ciphertext": values["refresh_token_ciphertext"],
            "expires_at": values["expires_at"],
            "refresh_lease_expires_at": None,
        }
        return True

    async def get_gitlab_connection(self, *, user_id: str) -> dict | None:
        if self.row is None or self.row.get("user_id") != user_id:
            return None
        row = dict(self.row)
        if self.lease_held:
            row["refresh_lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=30)
        else:
            row.setdefault("refresh_lease_expires_at", None)
        return row


@pytest.mark.asyncio
async def test_oauth_state_is_hashed_bound_and_single_use() -> None:
    store = MemoryStore()
    cipher = SourceControlCipher("encryption-key")
    state = await issue_oauth_state(
        store,
        user_id="user-a",
        provider="gitlab",
        cipher=cipher,
        code_verifier="verifier-secret",
    )

    persisted = next(iter(store.states.values()))
    assert state not in store.states
    assert persisted["code_verifier_ciphertext"] != "verifier-secret"

    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-b",
            provider="gitlab",
            cipher=cipher,
        )
    # A wrong user does not burn the rightful user's state.
    assert (
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="gitlab",
            cipher=cipher,
        )
        == "verifier-secret"
    )
    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="gitlab",
            cipher=cipher,
        )


@pytest.mark.asyncio
async def test_oauth_state_expiry_and_provider_tampering_fail_closed() -> None:
    store = MemoryStore()
    cipher = SourceControlCipher("encryption-key")
    state = await issue_oauth_state(store, user_id="user-a", provider="github", cipher=cipher)
    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="gitlab",
            cipher=cipher,
        )

    persisted = next(iter(store.states.values()))
    persisted["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="github",
            cipher=cipher,
        )


@pytest.mark.asyncio
async def test_github_state_does_not_require_the_gitlab_encryption_key() -> None:
    """GitHub persists no token or verifier, so an unset cipher must not disable it."""
    store = MemoryStore()
    state = await issue_oauth_state(store, user_id="user-a", provider="github")

    assert (
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="github",
        )
        is None
    )


def test_source_control_cipher_is_domain_separated_and_detects_tampering() -> None:
    cipher = SourceControlCipher("server-secret")
    encrypted = cipher.encrypt("glpat-raw-token")
    assert "glpat-raw-token" not in encrypted
    assert cipher.decrypt(encrypted) == "glpat-raw-token"

    with pytest.raises(SourceControlError):
        cipher.decrypt(encrypted[:-2] + "aa")
    with pytest.raises(SourceControlError):
        SourceControlCipher("")


def test_github_authorization_url_cannot_be_an_open_redirect() -> None:
    url = github_authorization_url(
        "https://github.com/login/oauth/authorize?client_id=abc", state="state-value"
    )
    assert parse_qs(urlparse(url).query)["state"] == ["state-value"]
    with pytest.raises(SourceControlError):
        github_authorization_url("https://evil.example/oauth", state="state-value")


def test_gitlab_config_requires_complete_credentials_and_safe_redirect() -> None:
    assert GitLabOAuthConfig.from_env({}) is None
    with pytest.raises(SourceControlError):
        GitLabOAuthConfig.from_env({"AGENT_GITLAB_OAUTH_CLIENT_ID": "only-one-value"})
    with pytest.raises(SourceControlError):
        GitLabOAuthConfig.from_env(
            {
                "AGENT_GITLAB_OAUTH_CLIENT_ID": "id",
                "AGENT_GITLAB_OAUTH_CLIENT_SECRET": "secret",
                "AGENT_GITLAB_OAUTH_REDIRECT_URI": "http://evil.example/callback",
            }
        )
    assert (
        GitLabOAuthConfig.from_env(
            {
                "AGENT_GITLAB_OAUTH_CLIENT_ID": "id",
                "AGENT_GITLAB_OAUTH_CLIENT_SECRET": "secret",
                "AGENT_GITLAB_OAUTH_REDIRECT_URI": "http://localhost:3001/callback",
            }
        )
        is not None
    )


@pytest.mark.asyncio
async def test_gitlab_pkce_exchange_identity_projects_refresh_and_revoke() -> None:
    calls: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode()) if request.content else {}
        calls.append((request.method, request.url.path, form))
        if request.url.path == "/oauth/token" and form.get("grant_type") == ["authorization_code"]:
            assert form["client_secret"] == ["client-secret"]
            assert form["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-one",
                    "refresh_token": "refresh-one",
                    "created_at": 1_700_000_000,
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/oauth/token" and form.get("grant_type") == ["refresh_token"]:
            assert form["refresh_token"] == ["refresh-one"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-two",
                    "refresh_token": "refresh-two",
                    "created_at": datetime.now(timezone.utc).timestamp(),
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/api/v4/user":
            assert request.headers["Authorization"] == "Bearer access-one"
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "username": "octo",
                    "name": "Octo Cat",
                    "web_url": "https://gitlab.com/octo",
                },
            )
        if request.url.path == "/api/v4/projects":
            assert request.url.params["membership"] == "true"
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
        if request.url.path == "/oauth/revoke":
            assert form["token"] == ["access-two"]
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    client = GitLabOAuthClient(
        GitLabOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://example.com/agents/connected?provider=gitlab",
        ),
        cipher=cipher,
        transport=httpx.MockTransport(handler),
    )

    authorization_url = await client.authorization_url(store, user_id="user-a")
    query = parse_qs(urlparse(authorization_url).query)
    assert query["scope"] == ["read_user read_api"]
    assert query["code_challenge_method"] == ["S256"]
    verifier = await consume_oauth_state(
        store,
        state=query["state"][0],
        user_id="user-a",
        provider="gitlab",
        cipher=cipher,
    )
    token = await client.exchange_code("callback-code", verifier or "")
    assert (await client.verify_user(token["access_token"]))["id"] == 42
    assert (await client.list_projects(token["access_token"]))[0]["name"] == "octo/project"

    connection = {
        "id": 7,
        "user_id": "user-a",
        "refresh_generation": 0,
        "refresh_lease_expires_at": None,
        "access_token_ciphertext": cipher.encrypt("access-one"),
        "refresh_token_ciphertext": cipher.encrypt("refresh-one"),
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    store.gitlab_refresh_claim = {
        "refresh_generation": 1,
        "refresh_token_ciphertext": connection["refresh_token_ciphertext"],
    }
    refreshed = await client.access_token_for_connection(store, connection)
    assert refreshed == "access-two"
    assert store.token_update is not None
    assert store.token_update["refresh_generation"] == 1
    assert "access-two" not in store.token_update["access_token_ciphertext"]
    assert store.token_update["refresh_generation"] == 1
    assert "refresh-two" not in store.token_update["refresh_token_ciphertext"]
    await client.revoke(refreshed)
    assert any(path == "/oauth/revoke" for _, path, _ in calls)


@pytest.mark.asyncio
async def test_gitlab_network_failures_are_safe_integration_errors() -> None:
    """A provider outage must not escape as an unhandled HTTP client exception."""

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider unavailable", request=request)

    client = GitLabOAuthClient(
        GitLabOAuthConfig("client", "secret", "https://example.com/callback"),
        cipher=SourceControlCipher("server-secret"),
        transport=httpx.MockTransport(unavailable),
    )

    with pytest.raises(SourceControlError, match="GitLab API request failed"):
        await client.verify_user("secret-access-token")


def _gitlab_client(handler, *, timeout_s: float | None = None) -> GitLabOAuthClient:
    return GitLabOAuthClient(
        GitLabOAuthConfig("client-id", "client-secret", "https://example.com/callback"),
        cipher=SourceControlCipher("server-secret"),
        timeout_s=timeout_s,
        transport=httpx.MockTransport(handler),
    )


def _connection(*, cipher: SourceControlCipher, access: str, refresh: str, expires_at: datetime):
    return {
        "id": 7,
        "user_id": "user-a",
        "access_token_ciphertext": cipher.encrypt(access),
        "refresh_token_ciphertext": cipher.encrypt(refresh),
        "expires_at": expires_at,
    }


@pytest.mark.asyncio
async def test_a_gitlab_token_outside_the_refresh_window_is_not_refreshed() -> None:
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url.path}")

    connection = _connection(
        cipher=cipher,
        access="access-live",
        refresh="refresh-live",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    token = await _gitlab_client(handler).access_token_for_connection(store, connection)
    assert token == "access-live"
    assert store.writes == []


@pytest.mark.asyncio
async def test_gitlab_refresh_lease_outlasts_a_delayed_provider_timeout_override(
    monkeypatch,
) -> None:
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    connection = _connection(
        cipher=cipher,
        access="access-old",
        refresh="refresh-old",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    store.row = connection

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = _gitlab_client(handler, timeout_s=300)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_refresh(_refresh_token: str, *, timeout_s: float) -> dict:
        assert timeout_s == 300
        started.set()
        await release.wait()
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "created_at": datetime.now(timezone.utc).timestamp(),
            "expires_in": 7200,
        }

    monkeypatch.setattr(client, "_refresh", delayed_refresh)
    pending = asyncio.create_task(client.access_token_for_connection(store, connection))
    await asyncio.wait_for(started.wait(), timeout=1)

    assert store.claimed_lease_seconds == 330
    assert not pending.done(), "the provider call should still hold the serialized account lock"

    release.set()
    assert await pending == "access-new"


@pytest.mark.asyncio
async def test_gitlab_refresh_pins_one_dynamic_timeout_for_lease_and_http(
    monkeypatch,
) -> None:
    """A setting increase during claim cannot lengthen this refresh request."""
    values = {"gitlab_request_timeout_s": 20.0}
    monkeypatch.setattr(
        source_control, "number_setting", lambda name, default=None: values.get(name, default)
    )
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    connection = _connection(
        cipher=cipher,
        access="access-old",
        refresh="refresh-old",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    store.row = connection
    store.after_claim = lambda: values.update(gitlab_request_timeout_s=300.0)
    request_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(
            200,
            json={
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "created_at": datetime.now(timezone.utc).timestamp(),
                "expires_in": 7200,
            },
        )

    client = _gitlab_client(handler)
    assert await client.access_token_for_connection(store, connection) == "access-new"
    assert store.claimed_lease_seconds == 50
    assert request_timeouts == [20.0]


@pytest.mark.asyncio
async def test_a_gitlab_refresh_without_the_lease_uses_the_stored_token() -> None:
    """The loser does not spend the refresh token or return the one it read."""
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    store.lease_held = True
    store.row = _connection(
        cipher=cipher,
        access="access-current",
        refresh="refresh-current",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    def winner_lands() -> None:
        store.lease_held = False

    store.after_failed_claim = winner_lands

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"refresh must not run: {request.url.path}")

    expired = _connection(
        cipher=cipher,
        access="access-stale",
        refresh="refresh-stale",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    token = await _gitlab_client(handler).access_token_for_connection(store, expired)
    assert token == "access-current"
    assert store.writes == []


@pytest.mark.asyncio
async def test_a_lost_gitlab_refresh_returns_the_replacement_not_its_own_token() -> None:
    """A reconnect that wins the generation keeps its token; the refresh is discarded."""
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    store.reject_write = True
    store.row = _connection(
        cipher=cipher,
        access="access-a",
        refresh="refresh-a",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    replacement = _connection(
        cipher=cipher,
        access="access-b",
        refresh="refresh-b",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )

    def reconnect() -> None:
        store.row = replacement

    store.after_rejected_write = reconnect

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        return httpx.Response(
            200,
            json={
                "access_token": "access-from-a",
                "refresh_token": "refresh-from-a",
                "created_at": datetime.now(timezone.utc).timestamp(),
                "expires_in": 7200,
            },
        )

    expired = _connection(
        cipher=cipher,
        access="access-a",
        refresh="refresh-a",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    token = await _gitlab_client(handler).access_token_for_connection(store, expired)
    assert token == "access-b"
    assert token != "access-from-a"
    assert store.token_update is None
    assert store.row is replacement
    [attempted] = store.writes
    assert cipher.decrypt(attempted["access_token_ciphertext"]) == "access-from-a"
    assert "access-from-a" not in replacement["access_token_ciphertext"]


@pytest.mark.asyncio
async def test_a_lost_gitlab_refresh_is_unavailable_when_nothing_valid_is_stored(
    monkeypatch,
) -> None:
    monkeypatch.setattr("cloud_agent.source_control._GITLAB_REFRESH_WAIT_INTERVAL_S", 0)
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    store.reject_write = True
    connection = _connection(
        cipher=cipher,
        access="access-old",
        refresh="refresh-old",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    store.row = connection

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "access-refreshed",
                "refresh_token": "refresh-refreshed",
                "created_at": datetime.now(timezone.utc).timestamp(),
                "expires_in": 7200,
            },
        )

    with pytest.raises(SourceControlError, match="token is being refreshed"):
        await _gitlab_client(handler).access_token_for_connection(store, connection)
    assert store.token_update is None
    assert cipher.decrypt(store.row["access_token_ciphertext"]) == "access-old"


@pytest.mark.asyncio
async def test_a_gitlab_refresh_without_the_lease_is_unavailable_once_the_token_lapses(
    monkeypatch,
) -> None:
    monkeypatch.setattr("cloud_agent.source_control._GITLAB_REFRESH_WAIT_INTERVAL_S", 0)
    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    store.lease_held = True
    connection = _connection(
        cipher=cipher,
        access="access-old",
        refresh="refresh-old",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    store.row = connection

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"refresh must not run: {request.url.path}")

    with pytest.raises(SourceControlError, match="token is being refreshed"):
        await _gitlab_client(handler).access_token_for_connection(store, connection)
    assert store.writes == []
