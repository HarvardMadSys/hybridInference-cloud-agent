"""Existing configurations and native host addresses survive the service rename."""

from __future__ import annotations

import pytest

from cloud_agent import settings
from cloud_agent.sandbox_manager_routing import (
    SandboxManagerUrlRefused,
    validate_sandbox_manager_url,
)
from cloud_agent.schemas import HostRegisterRequest
from cloud_agent.source_control import SourceControlCipher
from cloud_agent_host.chat_sandbox_markers import INITIAL_ARCHIVE_MARKER_ROOT
from cloud_agent_host.runner import _sandbox_manager_url_from_env
from cloud_agent_shared import sandbox_manager_compat
from cloud_agent_shared.sandbox_manager_compat import (
    LEGACY_ENV_NAMES,
    manager_env,
    manager_environment,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    settings.reset_settings()
    for current, legacy in LEGACY_ENV_NAMES.items():
        monkeypatch.delenv(current, raising=False)
        monkeypatch.delenv(legacy, raising=False)
    monkeypatch.setattr(sandbox_manager_compat, "_REPORTED_LEGACY_NAMES", set())
    yield
    settings.reset_settings()


@pytest.mark.parametrize("current,legacy", LEGACY_ENV_NAMES.items())
def test_existing_environment_is_accepted_and_explicit_new_value_wins(monkeypatch, current, legacy):
    monkeypatch.setenv(legacy, "previous-value")
    assert manager_env(current) == "previous-value"
    monkeypatch.setenv(current, "new-value")
    assert manager_env(current) == "new-value"
    monkeypatch.setenv(current, "")
    assert manager_env(current) == ""


def test_a_former_name_in_use_is_logged_once_and_the_current_name_never(monkeypatch, caplog):
    """The names can only be retired once nothing sets them, and this says what does."""
    caplog.set_level("WARNING", logger=sandbox_manager_compat.__name__)
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "new-value")
    assert manager_env("AGENT_SANDBOX_MANAGER_TOKEN") == "new-value"
    assert "sandbox_manager_legacy_env" not in caplog.text

    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_URL", "http://workspace-broker:8092")
    assert manager_env("AGENT_SANDBOX_MANAGER_URL") == "http://workspace-broker:8092"
    assert manager_env("AGENT_SANDBOX_MANAGER_URL") == "http://workspace-broker:8092"
    assert (
        manager_environment({"AGENT_WORKSPACE_BROKER_URL": "x"})["AGENT_SANDBOX_MANAGER_URL"] == "x"
    )
    assert caplog.text.count("sandbox_manager_legacy_env") == 1
    assert "name=AGENT_WORKSPACE_BROKER_URL replacement=AGENT_SANDBOX_MANAGER_URL" in caplog.text


def test_existing_encrypted_credential_keeps_its_storage_key_and_encryption_domain(monkeypatch):
    root_key = "sandbox-manager-test-root-key-at-least-32-bytes"
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", root_key)
    cipher = SourceControlCipher(
        root_key, domain=b"cloud-agent/admin-settings/v1/workspace_broker_token"
    )
    stored = {
        "workspace_broker_token": {"ciphertext": cipher.encrypt("old-manager-credential")},
        "broker_url_allowed_hosts": "node.example",
    }
    settings.apply_settings(1, stored)
    assert settings.text_setting("sandbox_manager_token") == "old-manager-credential"
    assert settings.text_setting("sandbox_manager_url_allowed_hosts") == "node.example"
    view = settings.settings_response(1, stored)
    assert view["values"]["sandbox_manager_token"] is None
    assert view["secret_configured"]["sandbox_manager_token"] is True
    assert "workspace_broker_token" not in view["values"]
    changed = settings.encode_changes({"sandbox_manager_token": "new-manager-credential"})
    assert set(changed) == {"workspace_broker_token"}
    assert (
        cipher.decrypt(changed["workspace_broker_token"]["ciphertext"]) == "new-manager-credential"
    )
    assert settings.encode_changes({"sandbox_manager_token": None}) == {
        "workspace_broker_token": ""
    }


def test_previous_settings_clients_can_update_the_same_secret(monkeypatch):
    monkeypatch.setenv(
        "AGENT_SETTINGS_ENCRYPTION_KEY", "test-root-key-longer-than-thirty-two-bytes"
    )
    changed = settings.encode_changes({"workspace_broker_token": "legacy-client-update"})
    settings.apply_settings(1, changed)
    assert settings.text_setting("sandbox_manager_token") == "legacy-client-update"
    with pytest.raises(ValueError, match="only once"):
        settings.encode_changes({"sandbox_manager_token": "new", "workspace_broker_token": "old"})


@pytest.mark.parametrize("field", ["broker_url", "sandbox_manager_url"])
def test_host_enrollment_accepts_both_address_spellings(field):
    request = HostRegisterRequest.model_validate(
        {
            "enroll_token": "test-token",
            "name": "node-a",
            "slots_total": 2,
            field: "https://node.example:8092",
        }
    )
    assert request.sandbox_manager_url == "https://node.example:8092"


def test_runner_can_advertise_an_existing_host_configuration(monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_URL", "http://workspace-broker:8092")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_ADVERTISE_URL", "http://node.example:8092")
    assert _sandbox_manager_url_from_env() == "http://node.example:8092"


def test_existing_containers_keep_their_durable_archive_fences():
    assert INITIAL_ARCHIVE_MARKER_ROOT == "/var/lib/freeinference-broker"


def test_old_compose_allowlist_accepts_the_new_service_alias_but_no_other_host(monkeypatch):
    monkeypatch.setenv("AGENT_BROKER_URL_ALLOWED_HOSTS", "workspace-broker")
    assert (
        validate_sandbox_manager_url("http://sandbox-manager:8092") == "http://sandbox-manager:8092"
    )
    assert (
        validate_sandbox_manager_url("http://workspace-broker:8092")
        == "http://workspace-broker:8092"
    )
    with pytest.raises(SandboxManagerUrlRefused):
        validate_sandbox_manager_url("http://other-service:8092")
