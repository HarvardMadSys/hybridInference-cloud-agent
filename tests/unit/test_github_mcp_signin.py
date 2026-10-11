"""Managed GitHub sign-in preserves per-user tokens and shared secret boundaries."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.unit.test_mcp_connections_api import (
    _ENCRYPTION_KEY,
    FakeStore,
    _client,
    _seed,
)

from cloud_agent.api import mcp_connections
from cloud_agent.mcp import github_oauth, oauth, refresh
from cloud_agent.mcp.registry import mcp_cipher, oauth_cipher

URL = "https://api.githubcopilot.com/mcp/readonly"
BASE = "https://agent.example.com/agents/api"


@pytest.fixture
def managed_app(monkeypatch):
    """Configure the service app and GitHub's actual published metadata."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)
    monkeypatch.setenv("AGENT_MCP_GITHUB_CLIENT_ID", "service-client")
    monkeypatch.setenv("AGENT_MCP_GITHUB_CLIENT_SECRET", "service-secret")
    monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://agent.example.com/agents")
    metadata = oauth.parse_authorization_server_metadata(
        {
            "issuer": github_oauth.ISSUER,
            "authorization_endpoint": github_oauth.AUTHORIZATION_ENDPOINT,
            "token_endpoint": github_oauth.TOKEN_ENDPOINT,
            "code_challenge_methods_supported": ["S256"],
        },
        expected_issuer=github_oauth.ISSUER,
    )
    discover = AsyncMock(
        return_value=(
            oauth.ProtectedResourceMetadata(
                resource=URL,
                scopes_supported=("repo", "read:org", "gist", "write:packages"),
            ),
            metadata,
        )
    )
    monkeypatch.setattr(oauth, "discover_authorization_server", discover)
    return metadata


def _flow_store(monkeypatch):
    """Persist flows with the same generation fence as the SQL repository."""
    store = FakeStore()
    connection_id = _seed(store, url=URL, template="github", auth_kind="oauth", auth_config={})

    async def save_flow(**kwargs):
        store.oauth_flow = kwargs
        store.oauth_state_hash = kwargs["state_hash"]
        store.connections[connection_id]["oauth_generation"] = kwargs["oauth_generation"]

    monkeypatch.setattr(store, "create_mcp_oauth_flow", save_flow, raising=False)
    return store, connection_id


def test_browser_signin_completes_without_user_client_credentials(managed_app, monkeypatch):
    """A stable callback uses single-use state and stores only the user's token."""
    store, connection_id = _flow_store(monkeypatch)
    exchange = AsyncMock(return_value={"access_token": "user-token", "refresh_token": "refresh"})
    monkeypatch.setattr(mcp_connections, "_post_form", exchange)
    with _client(store) as client:
        presets = client.get("/v1/agent/mcp/templates").json()
        assert presets[0]["auth_kind"] == "oauth"
        assert presets[0]["browser_sign_in_available"] is True
        assert "service-secret" not in str(presets)
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
        assert response.status_code == 200, response.text
        assert response.json()["needs_client_id"] is False
        assert "service-secret" not in response.text
        query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        assert query["client_id"] == ["service-client"]
        assert query["scope"] == ["repo read:org"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [f"{BASE}/v1/agent/mcp/oauth/callback/github"]
        params = {"state": query["state"][0], "code": "code"}
        callback = client.get(
            "/v1/agent/mcp/oauth/callback/github", params=params, follow_redirects=False
        )
        returned = parse_qs(urlsplit(callback.headers["location"]).query)
        assert returned["mcp_authorized"] == ["1"]
        assert returned["mcp_connection"] == [str(connection_id)]
        replay = client.get(
            "/v1/agent/mcp/oauth/callback/github", params=params, follow_redirects=False
        )
        assert "mcp_authorized=0" in replay.headers["location"]
        assert "service-secret" not in client.get("/v1/agent/mcp/connections").text
    exchange.assert_awaited_once()
    request = exchange.call_args.args[0]
    assert request.url == github_oauth.TOKEN_ENDPOINT
    assert request.form()["client_secret"] == "service-secret"
    row = store.connections[connection_id]
    assert row["client_secret_ciphertext"] is None
    assert row["auth_config"]["managed_github_oauth"] is True
    assert mcp_cipher(_ENCRYPTION_KEY).decrypt(row["access_token_ciphertext"]) == "user-token"
    renewal = refresh._build_request(row, cipher=mcp_cipher(_ENCRYPTION_KEY))
    assert renewal.form()["client_secret"] == "service-secret"


@pytest.mark.parametrize("field", ["issuer", "authorization_endpoint", "token_endpoint"])
def test_managed_secret_never_follows_unexpected_metadata(managed_app, monkeypatch, field):
    """The shared secret must never follow a discovery-supplied address."""
    from dataclasses import replace

    metadata = replace(managed_app, **{field: "https://attacker.example/token"})
    monkeypatch.setattr(
        oauth, "discover_authorization_server", AsyncMock(return_value=(None, metadata))
    )
    store, connection_id = _flow_store(monkeypatch)
    with _client(store) as client:
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
    assert response.status_code == 400
    assert "service-secret" not in response.text
    assert store.oauth_flow is None


@pytest.mark.parametrize(
    "url", ["https://attacker.example/mcp", "https://api.githubcopilot.com.attacker.example/mcp"]
)
def test_template_name_does_not_grant_shared_client_access(managed_app, monkeypatch, url):
    """A GitHub label or template on a custom URL does not authorize the app."""
    store, connection_id = _flow_store(monkeypatch)
    store.connections[connection_id]["url"] = url
    with _client(store) as client:
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
    assert response.status_code == 200
    assert response.json()["needs_client_id"] is True
    assert store.oauth_flow is None


@pytest.mark.parametrize("field", ["url", "resource", "issuer", "token_endpoint"])
def test_refresh_refuses_redirected_managed_connections(managed_app, field):
    """Editing a connection cannot turn its refresh into secret disclosure."""
    row = {
        "url": URL,
        "resource": URL,
        "issuer": github_oauth.ISSUER,
        "token_endpoint": github_oauth.TOKEN_ENDPOINT,
        "auth_config": {"client_id": "service-client", "managed_github_oauth": True},
    }
    row[field] = "https://attacker.example/token"
    with pytest.raises(refresh.RefreshRefused):
        refresh._build_request(row, cipher=mcp_cipher(_ENCRYPTION_KEY))


@pytest.mark.parametrize(
    "missing", ["AGENT_MCP_GITHUB_CLIENT_ID", "AGENT_MCP_GITHUB_CLIENT_SECRET"]
)
def test_unconfigured_browser_signin_keeps_token_fallback(managed_app, monkeypatch, missing):
    """Partial service setup must not advertise a working browser sign-in."""
    monkeypatch.delenv(missing)
    store, connection_id = _flow_store(monkeypatch)
    with _client(store) as client:
        preset = client.get("/v1/agent/mcp/connections").json()["templates"][0]
        assert preset["auth_kind"] == "static"
        assert preset["browser_sign_in_available"] is False
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
    assert response.status_code == 503
    assert "service administrator" in response.text
    assert store.oauth_flow is None


def test_managed_callback_rejects_wrong_state_and_other_connections(managed_app, monkeypatch):
    """The stable path cannot complete another user's connection without its state."""
    store, connection_id = _flow_store(monkeypatch)
    exchange = AsyncMock()
    monkeypatch.setattr(mcp_connections, "_post_form", exchange)
    with _client(store, user_id="user-other") as other_client:
        assert (
            other_client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize").status_code
            == 404
        )
    with _client(store) as client:
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
        state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
        for path, nonce in [("github", "wrong-state"), (str(connection_id + 1), state)]:
            callback = client.get(
                f"/v1/agent/mcp/oauth/callback/{path}",
                params={"state": nonce, "code": "code"},
                follow_redirects=False,
            )
            assert "mcp_authorized=0" in callback.headers["location"]
    exchange.assert_not_awaited()


def test_cancelled_github_signin_offers_a_friendly_retry(managed_app):
    """Declining consent gives users an understandable next step."""
    with _client(FakeStore()) as client:
        response = client.get(
            "/v1/agent/mcp/oauth/callback/github",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["mcp_authorized"] == ["0"]
    assert query["mcp_message"] == ["GitHub sign-in was cancelled. You can try again."]


def test_explicit_registration_can_replace_an_existing_managed_connection(managed_app, monkeypatch):
    """Keep existing API clients able to use their own registered app."""
    store, connection_id = _flow_store(monkeypatch)
    store.connections[connection_id]["auth_config"] = {
        "client_id": "service-client",
        "managed_github_oauth": True,
    }
    exchange = AsyncMock(return_value={"access_token": "own-app-token", "refresh_token": "refresh"})
    monkeypatch.setattr(mcp_connections, "_post_form", exchange)
    with _client(store) as client:
        response = client.post(
            f"/v1/agent/mcp/connections/{connection_id}/authorize",
            json={"client_id": "own-client", "client_secret": "own-secret"},
        )
        assert response.status_code == 200, response.text
        query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        assert query["client_id"] == ["own-client"]
        callback = client.get(
            f"/v1/agent/mcp/oauth/callback/{connection_id}",
            params={"state": query["state"][0], "code": "code"},
            follow_redirects=False,
        )
        assert "mcp_authorized=1" in callback.headers["location"]
    row = store.connections[connection_id]
    assert row["auth_config"]["managed_github_oauth"] is False
    renewal = refresh._build_request(row, cipher=mcp_cipher(_ENCRYPTION_KEY))
    assert renewal.form()["client_secret"] == "own-secret"


def test_retry_preserves_stored_custom_github_app(managed_app, monkeypatch):
    """A normal UI retry keeps an existing user's app identity and permissions."""
    store, connection_id = _flow_store(monkeypatch)
    cipher = mcp_cipher(_ENCRYPTION_KEY)
    client_cipher = oauth_cipher(_ENCRYPTION_KEY)
    store.connections[connection_id]["auth_config"] = {"client_id": "own-client"}
    store.connections[connection_id]["client_secret_ciphertext"] = client_cipher.encrypt(
        "own-secret"
    )
    exchange = AsyncMock(return_value={"access_token": "own-app-token", "refresh_token": "refresh"})
    monkeypatch.setattr(mcp_connections, "_post_form", exchange)
    with _client(store) as client:
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
        assert response.status_code == 200, response.text
        query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        assert query["client_id"] == ["own-client"]
        assert query["redirect_uri"] == [f"{BASE}/v1/agent/mcp/oauth/callback/{connection_id}"]
        callback = client.get(
            f"/v1/agent/mcp/oauth/callback/{connection_id}",
            params={"state": query["state"][0], "code": "code"},
            follow_redirects=False,
        )
        assert "mcp_authorized=1" in callback.headers["location"]
    assert exchange.call_args.args[0].form()["client_secret"] == "own-secret"
    row = store.connections[connection_id]
    assert row["auth_config"]["client_id"] == "own-client"
    assert not row["auth_config"].get("managed_github_oauth")
    assert client_cipher.decrypt(row["client_secret_ciphertext"]) == "own-secret"
    assert refresh._build_request(row, cipher=cipher).form()["client_secret"] == "own-secret"
