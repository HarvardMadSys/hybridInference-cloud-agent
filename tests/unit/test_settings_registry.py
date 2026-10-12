"""Settings import, runtime snapshots, encryption, and public display boundaries."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cloud_agent import settings
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent.source_control import SourceControlError


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    settings.reset_settings()
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.delenv("AGENT_COMPUTE_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-settings-session-key-32-bytes-long")
    yield
    settings.reset_settings()


class Repository:
    def __init__(self):
        self.revision = -1
        self.values: dict[str, Any] = {}
        self.seeds = 0

    async def read_settings(self):
        return self.revision, self.values

    async def seed_settings(self, values):
        self.seeds += 1
        if self.revision < 0:
            self.revision, self.values = 1, dict(values)
        else:
            merged = {**values, **self.values}
            if merged != self.values:
                self.revision += 1
                self.values = merged
        return self.revision, self.values


async def test_environment_is_imported_once_and_saved_reset_ignores_later_env(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_BASE_BRANCH", "release")
    repository = Repository()
    await settings.initialize_settings(repository)
    assert settings.text_setting("publish_base_branch") == "release"
    monkeypatch.setenv("AGENT_PUBLISH_BASE_BRANCH", "changed-env")
    await settings.initialize_settings(repository)
    assert repository.seeds == 1
    assert settings.setting_env("AGENT_PUBLISH_BASE_BRANCH") == "release"
    repository.values.update(settings.encode_changes({"publish_base_branch": None}))
    settings.apply_settings(2, repository.values)
    assert settings.setting_env("AGENT_PUBLISH_BASE_BRANCH") == "dev"
    assert settings.settings_environment()["AGENT_PUBLISH_BASE_BRANCH"] == "dev"
    assert __import__("os").environ["AGENT_PUBLISH_BASE_BRANCH"] == "changed-env"


def test_secrets_are_encrypted_imported_write_only_and_bound_to_their_key(monkeypatch):
    secret = "test-dispatch-secret-unique-value"
    monkeypatch.setenv("GATEWAY_GRANT_DISPATCH_TOKEN", secret)
    encoded = settings.initial_settings()
    assert secret not in json.dumps(encoded)
    settings.apply_settings(1, encoded)
    assert settings.setting_env("GATEWAY_GRANT_DISPATCH_TOKEN") == secret
    view = settings.settings_response(1, encoded)
    assert view["values"]["gateway_dispatch_token"] is None
    assert view["secret_configured"]["gateway_dispatch_token"] is True
    assert secret not in json.dumps(view)
    moved = {**encoded, "oncall_dispatch_token": encoded["gateway_dispatch_token"]}
    with pytest.raises(SourceControlError):
        settings.validate_stored_settings(moved)


def test_secret_configuration_requires_a_stable_key(monkeypatch):
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    with pytest.raises(SourceControlError):
        settings.encode_changes({"gateway_dispatch_token": "secret-must-not-be-persisted"})
    assert settings.encode_changes({"github_publish_token": None}) == {"github_publish_token": ""}


def test_stale_snapshots_do_not_revert_current_values():
    settings.apply_settings(3, {"site.app_name": "Current"})
    settings.apply_settings(2, {"site.app_name": "Old"})
    assert settings.text_setting("site.app_name") == "Current"


@pytest.mark.parametrize("value", [True, "5", float("inf"), float("nan"), 10**1000, -1])
def test_invalid_numeric_values_are_refused_without_echo(value):
    with pytest.raises(ValueError, match="Invalid value for"):
        settings.encode_changes({"max_attempts": value})


def test_merged_validation_refuses_inconsistent_limits():
    values = settings.initial_settings()
    values.update(settings.encode_changes({"attachments.max_total_bytes": 100}))
    with pytest.raises(ValueError, match="cannot exceed"):
        settings.validate_stored_settings(values)
    values = settings.initial_settings()
    values.update(
        settings.encode_changes(
            {"goals.arm_after_seconds": 1000, "goals.stalled_after_seconds": 60}
        )
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        settings.validate_stored_settings(values)


@pytest.mark.parametrize(
    "key", ["compute.cloudlab_api_url", "compute.registration_url", "mcp_registry_url"]
)
@pytest.mark.parametrize("url", ["http://example.com", "https://example.com?token=sensitive"])
def test_https_integrations_refuse_unusable_urls(key, url):
    with pytest.raises(ValueError) as cause:
        settings.encode_changes({key: url})
    assert "sensitive" not in str(cause.value)


@pytest.mark.parametrize(
    "key", [definition.key for definition in settings.SETTINGS if definition.url]
)
@pytest.mark.parametrize(
    "url",
    [
        "https://exa mple.com",
        "https://%",
        "https://percent%20host.example",
        "https://example.com/with space",
        "https://example.com/\tpath",
        "https://example.com/\npath",
        "https://example.com/\x7fpath",
        "https://127.0.0.999",
    ],
)
def test_saved_url_validation_refuses_hosts_and_text_the_browser_cannot_use(key, url):
    with pytest.raises(ValueError, match="Invalid value for") as cause:
        settings.encode_changes({key: url})
    assert url not in str(cause.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://control-plane:8000/api",
        "http://127.0.0.1:8000",
        "https://[2001:db8::1]:443/docs",
        "https://bücher.example/docs",
        "https://xn--bcher-kva.example/docs",
        "https://example.com/with%20space?lang=中文",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_saved_url_validation_preserves_ip_idna_internal_and_long_urls(url):
    assert settings.encode_changes({"site.org_url": url}) == {"site.org_url": url}


async def test_relay_never_seeds_a_partial_environment(monkeypatch):
    repository = Repository()
    reads = 0

    async def main_initializes(_delay):
        nonlocal reads
        reads += 1
        repository.revision, repository.values = (
            1,
            {**settings.initial_settings(), "publish_base_branch": "main-import"},
        )

    monkeypatch.setattr(settings.asyncio, "sleep", main_initializes)
    await settings.initialize_settings(repository, seed=False)
    assert repository.seeds == 0
    assert reads == 1
    assert settings.text_setting("publish_base_branch") == "main-import"


async def test_upgrade_imports_new_fields_once_without_reimporting_old_settings(monkeypatch):
    repository = Repository()
    repository.revision = 4
    repository.values = {"publish_base_branch": "saved-branch", "max_attempts": 3}
    monkeypatch.setenv("AGENT_PUBLISH_BASE_BRANCH", "stale-env-branch")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://original.example")
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "private-host-signing-key")
    await settings.initialize_settings(repository)
    assert repository.revision == 5
    assert settings.text_setting("publish_base_branch") == "saved-branch"
    assert settings.text_setting("gateway_issuer") == "https://original.example"
    assert settings.setting_env("AGENT_CONTROL_TOKEN_SECRET") == "private-host-signing-key"
    assert "private-host-signing-key" not in json.dumps(repository.values)
    monkeypatch.setenv("GATEWAY_ISSUER", "https://stale-new-env.example")
    await settings.initialize_settings(repository)
    assert repository.seeds == 1
    assert settings.text_setting("gateway_issuer") == "https://original.example"


def test_missing_required_setup_is_reported_without_exposing_saved_secrets(monkeypatch):
    monkeypatch.delenv("AGENT_SESSION_SECRET")
    encoded = settings.initial_settings()
    view = settings.settings_response(1, encoded)
    assert view["setup"]["required"] is True
    assert view["setup"]["bootstrap_missing"][0]["key"] == "AGENT_SETTINGS_ENCRYPTION_KEY"
    groups = {group["key"]: group for group in view["setup"]["groups"]}
    assert groups["sign_in"]["needs_setup"] is True
    assert groups["github"]["needs_setup"] is False
    assert {field["key"] for field in groups["sign_in"]["missing"]} >= {
        "gateway_issuer",
        "gateway_jwks_url",
    }
    monkeypatch.setenv(
        "AGENT_SETTINGS_ENCRYPTION_KEY", "test-root-key-longer-than-thirty-two-bytes"
    )
    encoded.update(settings.encode_changes({"github_app_client_secret": "test-hidden-secret"}))
    view = settings.settings_response(2, encoded)
    groups = {group["key"]: group for group in view["setup"]["groups"]}
    assert groups["github"]["needs_setup"] is True
    assert "test-hidden-secret" not in json.dumps(view)


def test_complete_required_setup_and_unused_optional_features_do_not_prompt():
    values = settings.initial_settings()
    values.update(
        settings.encode_changes(
            {
                "gateway_base_url": "https://gateway.example",
                "gateway_dispatch_token": "test-dispatch-key",
                "gateway_issuer": "https://identity.example",
                "gateway_jwks_url": "https://gateway.example/jwks",
                "gateway_authorize_url": "https://gateway.example/authorize",
                "session_redirect_uri": "https://agents.example/callback",
                "web_base_url": "https://agents.example",
                "control_signing_secret": "test-control-key",
                "workspace_broker_token": "test-broker-key",
                "source_control_encryption_key": "test-user-credential-key",
            }
        )
    )
    assert settings.settings_response(1, values)["setup"]["required"] is False


@pytest.mark.parametrize("key", ["source_control_encryption_key", "compute_encryption_key"])
def test_existing_credential_encryption_keys_cannot_be_reset_or_replaced(key):
    encoded = settings.encode_changes({key: "test-encryption-key-at-least-thirty-two-bytes"})
    settings.validate_settings_update({}, encoded)
    settings.validate_settings_update(encoded, encoded)
    for change in (None, "test-replacement-key-at-least-thirty-two-bytes"):
        replacement = {**encoded, **settings.encode_changes({key: change})}
        with pytest.raises(ValueError, match="re-encrypting stored credentials"):
            settings.validate_settings_update(encoded, replacement)


async def test_public_site_settings_expose_only_display_allowlist():
    settings.apply_settings(
        1,
        {
            **settings.initial_settings(),
            "site.app_name": "Configured app",
            "site.feature_rag": True,
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/v1/agent/site-config")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    view = response.json()
    assert set(view) == {"branding", "features"}
    assert view["branding"]["appName"] == "Configured app"
    assert view["features"] == {"rag": True}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mcp_app()), base_url="http://relay"
    ) as client:
        assert (await client.get("/v1/agent/site-config")).status_code == 404


@pytest.mark.parametrize(
    "prefix",
    ["", "/", "relative", "//gateway.example/path", "/grants?x=1", "/grants#x", "/bad path"],
)
def test_invalid_grant_prefix_cannot_be_saved_and_break_client_startup(prefix):
    with pytest.raises(ValueError, match="Grant API path"):
        settings.encode_changes({"gateway_grant_base_path": prefix})


@pytest.mark.parametrize("prefix", ["/internal/agent-grants", "/custom/grants/"])
def test_saved_grant_prefix_can_be_loaded_by_a_gateway_client(prefix):
    from cloud_agent.grants import GrantClient

    settings.apply_settings(1, settings.encode_changes({"gateway_grant_base_path": prefix}))
    client = GrantClient("https://gateway.example", "test-credential")
    assert client._base_path == prefix.rstrip("/")
