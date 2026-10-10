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
        repository.revision, repository.values = 1, {"publish_base_branch": "main-import"}

    monkeypatch.setattr(settings.asyncio, "sleep", main_initializes)
    await settings.initialize_settings(repository, seed=False)
    assert repository.seeds == 0
    assert reads == 1
    assert settings.text_setting("publish_base_branch") == "main-import"


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
