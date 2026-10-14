"""Settings import, runtime snapshots, encryption, and public display boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
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
        settings.encode_changes({"gateway_dispatch_token": "secret-that-must-never-be-persisted"})
    assert settings.encode_changes({"github_publish_token": None}) == {"github_publish_token": ""}


def test_stale_snapshots_do_not_revert_current_values():
    settings.apply_settings(3, {"site.app_name": "Current"})
    settings.apply_settings(2, {"site.app_name": "Old"})
    assert settings.text_setting("site.app_name") == "Current"


@pytest.mark.parametrize("value", [True, "5", float("inf"), float("nan"), 10**1000, -1])
def test_invalid_numeric_values_are_refused_without_echo(value):
    with pytest.raises(ValueError, match="Invalid value for"):
        settings.encode_changes({"max_attempts": value})


def test_what_an_owner_may_keep_cannot_be_less_than_one_turn():
    values = settings.initial_settings()
    values.update(settings.encode_changes({"attachments.max_user_bytes": 1024}))
    with pytest.raises(ValueError, match="cannot exceed"):
        settings.validate_stored_settings(values)
    values.update(settings.encode_changes({"attachments.max_total_bytes": 1024}))
    values.update(settings.encode_changes({"attachments.max_file_bytes": 1024}))
    settings.validate_stored_settings(values)


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


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == settings.__name__ and record.levelno == logging.WARNING
    ]


async def test_a_stored_value_this_release_refuses_does_not_stop_startup(caplog):
    """A narrower bound in a later release must not lock every listener out.

    The value falls back to its default, and the key — never the value — is
    logged once for the revision that carries it, not on every synchronization.
    """
    repository = Repository()
    repository.revision = 3
    repository.values = {**settings.initial_settings(), "task_titles.max_chars": 48}
    caplog.set_level(logging.WARNING, logger=settings.__name__)

    await settings.initialize_settings(repository)

    assert settings.integer_setting("task_titles.max_chars") == 36
    view = settings.settings_response(3, repository.values)
    assert view["values"]["task_titles.max_chars"] == 36
    logged = _warnings(caplog)
    assert len(logged) == 1
    assert "task_titles.max_chars" in logged[0]
    assert "48" not in logged[0]
    settings.apply_settings(3, repository.values)
    assert len(_warnings(caplog)) == 1


def test_one_key_at_a_time_repairs_every_stored_value_this_release_refuses():
    """The editor saves one key per request; neither bad key may block the other."""
    stored = {
        **settings.initial_settings(),
        "task_titles.max_chars": 48,
        "max_attempts": 50,
    }
    for key in ("task_titles.max_chars", "max_attempts"):
        update = {**stored, **settings.encode_changes({key: None})}
        settings.validate_settings_update(stored, update)
        stored = update
    unrelated = {**stored, **settings.encode_changes({"publish_base_branch": "main"})}
    settings.validate_settings_update(stored, unrelated)


def test_a_stored_pair_out_of_order_falls_back_to_both_defaults():
    stored = {
        **settings.initial_settings(),
        "task_titles.timeout_seconds": 100.0,
        "task_titles.total_timeout_seconds": 50.0,
    }
    settings.apply_settings(1, stored)
    assert settings.number_setting("task_titles.timeout_seconds") == 10.0
    assert settings.number_setting("task_titles.total_timeout_seconds") == 120.0
    # A pair the update does not touch does not block it either.
    settings.validate_settings_update(
        stored, {**stored, **settings.encode_changes({"publish_base_branch": "main"})}
    )


def test_a_stored_key_shorter_than_its_minimum_is_kept_and_reported(caplog):
    """The default is no key at all, which would orphan everything it signed."""
    definition = settings.SETTINGS_BY_KEY["session_signing_secret"]
    stored = {
        **settings.initial_settings(),
        "session_signing_secret": settings._encode(definition, "a-short-saved-key"),
    }
    caplog.set_level(logging.WARNING, logger=settings.__name__)

    settings.apply_settings(1, stored)

    assert settings.setting_env("AGENT_SESSION_SECRET") == "a-short-saved-key"
    logged = _warnings(caplog)
    assert len(logged) == 1
    assert "session_signing_secret" in logged[0]
    assert "a-short-saved-key" not in logged[0]
    settings.validate_settings_update(
        stored, {**stored, **settings.encode_changes({"publish_base_branch": "main"})}
    )
    with pytest.raises(ValueError, match="at least 32 bytes"):
        settings.encode_changes({"session_signing_secret": "another-short-key"})


_KEYS_WITH_A_MINIMUM = (
    "control_signing_secret",
    "source_control_encryption_key",
    "gateway_dispatch_token",
    "oncall_dispatch_token",
)


@pytest.mark.parametrize("key", _KEYS_WITH_A_MINIMUM)
def test_a_short_signing_key_or_credential_is_refused_when_new_and_reported_when_saved(
    key, monkeypatch
):
    """The HMAC key's tokens sit inside every sandbox, a ready offline oracle.

    The editor and the API refuse a value a person could have typed; the editor
    reads the floor from the definition. A value the environment supplied
    before the floor existed still loads, as a saved one does, and is reported
    by name until it is replaced.
    """
    definition = settings.SETTINGS_BY_KEY[key]
    assert definition.definition()["minimum_bytes"] == 32
    with pytest.raises(ValueError, match="at least 32 bytes"):
        settings.encode_changes({key: "cloudagent2026"})
    assert settings.encode_changes({key: "k" * 32})[key]
    assert definition.env
    monkeypatch.setenv(definition.env, "k" * 31)
    settings.apply_settings(1, settings.initial_settings())
    assert settings.text_setting(key) == "k" * 31
    assert definition.env in settings.weak_credentials()


def test_a_short_signing_key_saved_before_the_minimum_keeps_its_hosts_working():
    """Refusing it on load would invalidate every host and worker credential."""
    from cloud_agent import tokens

    definition = settings.SETTINGS_BY_KEY["control_signing_secret"]
    stored = {
        **settings.initial_settings(),
        "control_signing_secret": settings._encode(definition, "a-short-legacy-key"),
    }
    settings.apply_settings(1, stored)
    minted = tokens.mint_worker_token(job_id="ajob_1", attempt_id=1, lease_generation=1)
    assert tokens.parse_worker_token(minted)["job_id"] == "ajob_1"


def test_an_update_still_cannot_save_an_ordering_it_breaks():
    stored = settings.initial_settings()
    with pytest.raises(ValueError, match="cannot exceed"):
        settings.validate_settings_update(
            stored, {**stored, **settings.encode_changes({"goals.arm_after_seconds": 1000})}
        )


def test_an_undecryptable_stored_secret_still_fails_closed(monkeypatch):
    monkeypatch.setenv("GATEWAY_GRANT_DISPATCH_TOKEN", "test-dispatch-secret-unique-value")
    encoded = settings.initial_settings()
    moved = {**encoded, "oncall_dispatch_token": encoded["gateway_dispatch_token"]}
    with pytest.raises(SourceControlError):
        settings.apply_settings(1, moved)
    with pytest.raises(ValueError, match="Unencrypted stored secret"):
        settings.apply_settings(1, {**encoded, "oncall_dispatch_token": "plaintext"})


async def test_a_failed_synchronization_names_the_setting_in_its_log(monkeypatch, caplog):
    """The message names a setting and never its value, so it is safe to log."""
    repository = Repository()
    repository.revision = 2
    repository.values = {**settings.initial_settings(), "oncall_dispatch_token": "plaintext"}
    ticks = 0

    async def tick(_delay):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(settings.asyncio, "sleep", tick)
    caplog.set_level(logging.WARNING, logger=settings.__name__)
    with pytest.raises(asyncio.CancelledError):
        await settings.synchronize_settings(repository)
    assert _warnings(caplog) == [
        "settings synchronization failed "
        "(ValueError: Unencrypted stored secret for On-call dispatch token)"
    ]


async def test_a_secret_that_cannot_be_decrypted_is_named_in_the_log(monkeypatch, caplog):
    """Which stored credential failed is what an operator can act on; never its value."""
    monkeypatch.setenv("GATEWAY_GRANT_DISPATCH_TOKEN", "test-dispatch-secret-unique-value")
    encoded = settings.initial_settings()
    repository = Repository()
    repository.revision = 2
    repository.values = {**encoded, "oncall_dispatch_token": encoded["gateway_dispatch_token"]}
    ticks = 0

    async def tick(_delay):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(settings.asyncio, "sleep", tick)
    caplog.set_level(logging.WARNING, logger=settings.__name__)
    with pytest.raises(asyncio.CancelledError):
        await settings.synchronize_settings(repository)
    assert _warnings(caplog) == [
        "settings synchronization failed "
        "(SourceControlError: Stored secret for On-call dispatch token could not be decrypted)"
    ]


@pytest.mark.parametrize(
    "key", ["compute.cloudlab_api_url", "compute.registration_url", "mcp_registry_url"]
)
@pytest.mark.parametrize("url", ["http://example.com", "https://example.com?token=sensitive"])
def test_https_integrations_refuse_unusable_urls(key, url):
    with pytest.raises(ValueError) as cause:
        settings.encode_changes({key: url})
    assert "sensitive" not in str(cause.value)


@pytest.mark.parametrize(
    ("key", "url"),
    [
        ("github_app_install_url", "https://example.com/apps/agent/installations/new"),
        ("github_app_install_url", "http://github.com/apps/agent/installations/new"),
        ("github_app_install_url", "https://github.com:8443/apps/agent/installations/new"),
        ("gitlab_redirect_uri", "http://agents.example.org/api/v1/agent/gitlab/callback"),
    ],
)
def test_source_control_urls_their_flows_refuse_cannot_be_saved(key, url):
    """Saved, then reported as configured, then refused when a user clicks Connect."""
    with pytest.raises(ValueError, match="Invalid value for"):
        settings.encode_changes({key: url})


@pytest.mark.parametrize(
    ("key", "url"),
    [
        ("github_app_install_url", "https://github.com/apps/agent/installations/new"),
        ("gitlab_redirect_uri", "https://agents.example.org/api/v1/agent/gitlab/callback"),
        ("gitlab_redirect_uri", "http://localhost:3001/api/v1/agent/gitlab/callback"),
    ],
)
def test_source_control_urls_their_flows_accept_can_be_saved(key, url):
    assert settings.encode_changes({key: url}) == {key: url}


def test_a_stored_install_url_the_connect_flow_refuses_reads_as_unconfigured():
    """Saved before the rule existed: setup asks for it instead of claiming it works."""
    stored = {
        **settings.initial_settings(),
        **settings.encode_changes(
            {
                "github_app_id": "12345",
                "github_app_client_id": "Iv1.client",
                "github_app_client_secret": "test-client-secret",
                "github_app_private_key": "test-private-key",
                "source_control_encryption_key": "test-user-credential-key-of-32-bytes",
            }
        ),
        "github_app_install_url": "https://example.com/apps/agent/installations/new",
    }
    view = settings.settings_response(1, stored)
    github = next(group for group in view["setup"]["groups"] if group["key"] == "github")
    assert github["configured"] is False
    assert {field["key"] for field in github["missing"]} == {"github_app_install_url"}


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
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "private-host-signing-key-of-32-bytes")
    await settings.initialize_settings(repository)
    assert repository.revision == 5
    assert settings.text_setting("publish_base_branch") == "saved-branch"
    assert settings.text_setting("gateway_issuer") == "https://original.example"
    assert (
        settings.setting_env("AGENT_CONTROL_TOKEN_SECRET") == "private-host-signing-key-of-32-bytes"
    )
    assert "private-host-signing-key-of-32-bytes" not in json.dumps(repository.values)
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
                "gateway_dispatch_token": "test-dispatch-key-of-at-least-32-bytes",
                "gateway_issuer": "https://identity.example",
                "gateway_jwks_url": "https://gateway.example/jwks",
                "gateway_authorize_url": "https://gateway.example/authorize",
                "session_redirect_uri": "https://agents.example/callback",
                "web_base_url": "https://agents.example",
                "control_signing_secret": "test-control-key-longer-than-thirty-two-bytes",
                "sandbox_manager_token": "test-sandbox-manager-key",
                "source_control_encryption_key": "test-user-credential-key-of-32-bytes",
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


def _saved_gateway() -> dict[str, Any]:
    return {
        **settings.initial_settings(),
        **settings.encode_changes(
            {
                "gateway_base_url": "https://gateway.example",
                "gateway_dispatch_token": "gateway-dispatch-token-of-32-bytes",
            }
        ),
    }


def _moved(stored: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**stored, **settings.encode_changes(changes)}


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example",
        "http://localhost:8080",
        "https://gateway.example:8443",
    ],
)
def test_the_saved_dispatch_token_does_not_follow_the_gateway_url_to_another_origin(url):
    """The token is write-only; an editor must not be able to send it elsewhere.

    It authorizes minting grants for any user at the Gateway. Moving the URL it
    is sent to, and leaving it saved, would deliver it to whoever owns the new
    host, whatever the editor could read.
    """
    stored = _saved_gateway()
    with pytest.raises(ValueError, match="Gateway dispatch token"):
        settings.validate_settings_update(stored, _moved(stored, gateway_base_url=url))
    # Nor in two steps, by clearing the URL and then naming a new one.
    cleared = _moved(stored, gateway_base_url=None)
    settings.validate_settings_update(stored, cleared)
    with pytest.raises(ValueError, match="Gateway dispatch token"):
        settings.validate_settings_update(cleared, _moved(cleared, gateway_base_url=url))


def test_the_gateway_url_moves_when_its_token_is_reset_or_replaced_with_it():
    stored = _saved_gateway()
    for token in (None, "replacement-dispatch-token-of-32-bytes"):
        settings.validate_settings_update(
            stored,
            _moved(stored, gateway_base_url="https://new.example", gateway_dispatch_token=token),
        )
    unbound = _moved(settings.initial_settings(), gateway_base_url="https://gateway.example")
    settings.validate_settings_update(
        unbound, _moved(unbound, gateway_base_url="https://new.example")
    )


async def test_public_site_settings_expose_only_display_allowlist():
    settings.apply_settings(
        1,
        {
            **settings.initial_settings(),
            "site.app_name": "Configured app",
            "site.docs_url": "https://docs.example.com/agents/",
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
    assert set(view["branding"]) == {
        "appName",
        "orgName",
        "orgUrl",
        "statusUrl",
        "docsUrl",
        "gatewayUrl",
    }
    assert view["branding"]["appName"] == "Configured app"
    assert view["branding"]["docsUrl"] == "https://docs.example.com/agents/"
    assert view["features"] == {"rag": True}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mcp_app()), base_url="http://relay"
    ) as client:
        assert (await client.get("/v1/agent/site-config")).status_code == 404


@pytest.mark.parametrize("value", ["javascript:alert(1)", "docs.example.com", "ftp://docs.example"])
def test_documentation_link_must_be_an_http_url(value):
    with pytest.raises(ValueError):
        settings.encode_changes({"site.docs_url": value})


def test_documentation_link_is_imported_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_WEB_DOCS_URL", "https://docs.example.com/agents/")
    assert settings.initial_settings()["site.docs_url"] == "https://docs.example.com/agents/"
    monkeypatch.delenv("AGENT_WEB_DOCS_URL")
    assert settings.initial_settings()["site.docs_url"] == ""


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


def test_a_new_control_signing_key_must_be_at_least_32_bytes():
    """Every sandbox holds a worker token this key signed: a short one is guessable."""
    definition = settings.SETTINGS_BY_KEY["control_signing_secret"].definition()
    assert definition["minimum_bytes"] == settings.MINIMUM_CREDENTIAL_BYTES == 32
    with pytest.raises(ValueError, match="Invalid value for") as cause:
        settings.encode_changes({"control_signing_secret": "short-host-signing-key"})
    assert "short-host-signing-key" not in str(cause.value)
    assert settings.encode_changes({"control_signing_secret": "x" * 32})
    # Clearing it is still a supported answer: hosts then cannot enroll at all.
    assert settings.encode_changes({"control_signing_secret": None}) == {
        "control_signing_secret": ""
    }


async def test_a_short_control_signing_key_saved_before_the_floor_still_loads_and_warns(
    monkeypatch, caplog
):
    """An upgrade must not lock a deployment out of the page that would replace it."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "legacy-host-signing-key")
    monkeypatch.setenv("ADMIN_TOKEN", "legacy-admin-token")
    repository = Repository()
    with caplog.at_level("WARNING", logger="cloud_agent.settings"):
        await settings.initialize_settings(repository)
    assert settings.setting_env("AGENT_CONTROL_TOKEN_SECRET") == "legacy-host-signing-key"
    # The saved value is decoded again on every synchronization.
    settings.apply_settings(repository.revision + 1, repository.values)
    warned = {
        getattr(record, "credential", None)
        for record in caplog.records
        if getattr(record, "event", None) == "agent_weak_credential"
    }
    assert warned == {"AGENT_CONTROL_TOKEN_SECRET", "ADMIN_TOKEN"}
    assert "legacy-host-signing-key" not in caplog.text
    assert "legacy-admin-token" not in caplog.text


async def test_credentials_at_the_floor_are_not_reported(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "c" * 32)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    with caplog.at_level("WARNING", logger="cloud_agent.settings"):
        await settings.initialize_settings(Repository())
    assert settings.weak_credentials() == []
    assert "agent_weak_credential" not in caplog.text


@pytest.mark.parametrize("key", ["gateway_base_url", "gateway_jwks_url", "gateway_authorize_url"])
@pytest.mark.parametrize(
    "url",
    ["http://gateway.example", "http://10.0.0.5:8080", "http://gateway.example.localhost.evil"],
)
def test_a_new_gateway_trust_url_must_use_https(key, url):
    """The dispatch token, the identity keys and sign-in itself travel to these."""
    with pytest.raises(ValueError, match="Invalid value for") as cause:
        settings.encode_changes({key: url})
    assert url not in str(cause.value)
    https = url.replace("http://", "https://", 1)
    assert settings.encode_changes({key: https}) == {key: https}


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://gateway.localhost",
        "http://127.0.0.1:8080/v1/identity/jwks",
        "http://[::1]:8080",
        "http://local-gateway:8080",
    ],
)
def test_plain_http_stays_available_for_this_machine_and_compose_names(url):
    assert settings.encode_changes({"gateway_base_url": url}) == {"gateway_base_url": url}


def test_a_plain_http_gateway_url_saved_before_the_rule_still_loads(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gateway.internal.example:8080")
    encoded = settings.initial_settings()
    settings.apply_settings(1, encoded)
    assert settings.text_setting("gateway_base_url") == "http://gateway.internal.example:8080"
    # And an unrelated change to the same snapshot is still accepted.
    settings.validate_settings_update(
        encoded, {**encoded, **settings.encode_changes({"publish_base_branch": "main"})}
    )


def _bound_gateway() -> dict[str, Any]:
    return {
        **settings.initial_settings(),
        **settings.encode_changes(
            {
                "gateway_base_url": "https://gateway.example",
                "gateway_dispatch_token": "test-dispatch-token-for-this-gateway",
            }
        ),
    }


def test_moving_the_gateway_url_requires_the_dispatch_token_again():
    """A write-only token must not be readable by pointing its destination elsewhere."""
    previous = _bound_gateway()
    moved = {**previous, **settings.encode_changes({"gateway_base_url": "https://other.example"})}
    with pytest.raises(ValueError, match="clear Gateway dispatch token first") as cause:
        settings.validate_settings_update(previous, moved)
    assert "other.example" not in str(cause.value)


@pytest.mark.parametrize(
    "token", ["test-dispatch-token-for-this-gateway", "test-dispatch-token-for-the-new-one", None]
)
def test_moving_the_gateway_url_with_the_token_supplied_or_cleared_is_accepted(token):
    previous = _bound_gateway()
    changes = settings.encode_changes(
        {"gateway_base_url": "https://other.example", "gateway_dispatch_token": token}
    )
    settings.validate_settings_update(previous, {**previous, **changes})


def test_clearing_the_gateway_url_or_changing_something_else_needs_no_token():
    previous = _bound_gateway()
    for change in ({"gateway_base_url": None}, {"gateway_request_timeout_s": 20.0}):
        settings.validate_settings_update(previous, {**previous, **settings.encode_changes(change)})


def test_the_settings_page_can_move_the_gateway_one_field_at_a_time():
    """The page saves one field per request: clear, move, then enter the token again."""
    current = _bound_gateway()
    for change in (
        {"gateway_dispatch_token": None},
        {"gateway_base_url": "https://other.example"},
        {"gateway_dispatch_token": "test-dispatch-token-for-the-new-one"},
    ):
        following = {**current, **settings.encode_changes(change)}
        settings.validate_settings_update(current, following)
        current = following
