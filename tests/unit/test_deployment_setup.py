"""Deployment setup metadata, state snapshots, and settings-root compatibility."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_agent import deployment_setup, settings
from cloud_agent.settings import SettingsEncryptionError

ROOT = "sentinel-dedicated-root-key-of-32-bytes"
FALLBACK = "sentinel-fallback-session-key-of-32-bytes"
OTHER_ROOT = "sentinel-different-root-key-of-32-bytes"
TOKEN = "sentinel-admin-token-of-at-least-32-bytes"
INLINE_KEY = "sentinel-inline-github-private-key"
MOUNT_PATH = "/sensitive/mounted/github-app.pem"
COMPUTE_PATH = "/sensitive/compute/profiles.json"
PGADMIN_URL = "not a url :: sentinel"

_BOOTSTRAP_ENVS = (
    "AGENT_SETTINGS_ENCRYPTION_KEY",
    "AGENT_SESSION_SECRET",
    "ADMIN_TOKEN",
    "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
    "AGENT_COMPUTE_CONFIG",
    "AGENT_PGADMIN_BOOTSTRAP_URL",
    "AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL",
    "AGENT_PGADMIN_BOOTSTRAP_DIR",
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    settings.reset_settings()
    for name in _BOOTSTRAP_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield
    settings.reset_settings()


def _states(response: dict[str, Any]) -> dict[str, Any]:
    return {row["id"]: row["state"] for row in response["bootstrap"]}


def test_legacy_bootstrap_keys_and_order_are_preserved() -> None:
    assert [row["key"] for row in deployment_setup.BOOTSTRAP] == [
        "AGENT_DATABASE_URL",
        "AGENT_SETTINGS_ENCRYPTION_KEY / AGENT_SESSION_SECRET",
        "ADMIN_TOKEN",
        "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
        "AGENT_COMPUTE_CONFIG",
        "AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL / "
        "AGENT_PGADMIN_BOOTSTRAP_DIR",
        "Host networking, isolation, listener and build configuration",
    ]
    assert all({"key", "label", "description"} <= row.keys() for row in deployment_setup.BOOTSTRAP)


def test_actions_use_only_fixed_targets_and_database_targets_the_deployment_guide() -> None:
    rows = deployment_setup.bootstrap_snapshot({})
    targets = {
        action["target"]
        for row in rows
        for action in row["actions"]  # type: ignore[index]
    }
    assert targets <= deployment_setup.KNOWN_DEPLOYMENT_SETUP_TARGETS
    database = next(row for row in rows if row["id"] == "database")
    assert database["actions"] == [  # type: ignore[index]
        {"target": "deployment_guide", "label": "View deployment guide"}
    ]
    assert "Admin Database" not in database["description"]
    root = next(row for row in rows if row["id"] == "settings_root_key")
    assert "data encrypted under that root" in root["description"]
    assert "all stored data" not in root["description"]


def test_snapshots_do_not_share_mutable_rows_or_action_lists() -> None:
    first = deployment_setup.bootstrap_snapshot({})
    second = deployment_setup.bootstrap_snapshot({})
    first[0]["description"] = "changed"
    first[6]["actions"].append({"target": "database", "label": "injected"})  # type: ignore[index]

    assert second[0]["description"] != "changed"
    assert len(second[6]["actions"]) == 1  # type: ignore[arg-type]
    assert len(deployment_setup.BOOTSTRAP[6]["actions"]) == 1
    assert deployment_setup.BOOTSTRAP[0]["description"] != "changed"


@pytest.mark.parametrize(
    ("dedicated", "fallback", "expected"),
    [
        (None, None, "missing"),
        ("short", FALLBACK, "missing"),
        (None, "short", "missing"),
        (None, FALLBACK, "fallback"),
        (ROOT, None, "configured"),
        ("short", None, "missing"),
    ],
)
def test_root_key_state_uses_the_key_the_cipher_resolves(
    monkeypatch: pytest.MonkeyPatch, dedicated: str | None, fallback: str | None, expected: str
) -> None:
    if dedicated is not None:
        monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", dedicated)
    if fallback is not None:
        monkeypatch.setenv("AGENT_SESSION_SECRET", fallback)
    assert _states(settings.settings_response(1, {}))["settings_root_key"] == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [(None, "not_used"), ("", "not_used"), ("short", "weak"), (TOKEN, "configured")],
)
def test_admin_token_state_is_configuration_not_authentication_health(
    monkeypatch: pytest.MonkeyPatch, token: str | None, expected: str
) -> None:
    if token is not None:
        monkeypatch.setenv("ADMIN_TOKEN", token)
    assert _states(settings.settings_response(1, {}))["recovery_token"] == expected


def test_unused_and_unverified_deployment_features_are_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = _states(settings.settings_response(1, {}))
    assert states["github_app_key_file"] == "not_used"
    assert states["compute_config_file"] == "not_used"
    assert states["pgadmin_bootstrap"] == "not_used"
    assert states["database"] == "configured"
    assert states["infrastructure"] == "unknown"

    monkeypatch.setenv("AGENT_GITHUB_APP_PRIVATE_KEY_PATH", MOUNT_PATH)
    monkeypatch.setenv("AGENT_COMPUTE_CONFIG", COMPUTE_PATH)
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL", PGADMIN_URL)
    states = _states(settings.settings_response(1, {}))
    assert states["github_app_key_file"] == "unknown"
    assert states["compute_config_file"] == "unknown"
    assert states["pgadmin_bootstrap"] == "unknown"


def test_saved_inline_github_key_marks_the_mounted_file_unused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", ROOT)
    encoded = settings.encode_changes({"github_app_private_key": INLINE_KEY})
    response = settings.settings_response(1, encoded)
    assert _states(response)["github_app_key_file"] == "not_used"
    assert response["secret_configured"]["github_app_private_key"] is True


def test_malformed_deployment_values_never_break_settings_or_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", ROOT)
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_GITHUB_APP_PRIVATE_KEY_PATH", MOUNT_PATH)
    monkeypatch.setenv("AGENT_COMPUTE_CONFIG", COMPUTE_PATH)
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_URL", PGADMIN_URL)
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_DIR", "/sensitive/pgadmin/volume")

    response = settings.settings_response(1, {})
    serialized = json.dumps(response)
    assert _states(response)["compute_config_file"] == "unknown"
    assert _states(response)["pgadmin_bootstrap"] == "unknown"
    for sentinel in (ROOT, TOKEN, MOUNT_PATH, COMPUTE_PATH, PGADMIN_URL, "/sensitive/pgadmin"):
        assert sentinel not in serialized


def test_one_state_failure_keeps_the_editor_and_other_states_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_values):
        raise ValueError("sentinel-private-diagnostic")

    monkeypatch.setitem(deployment_setup._STATE_RESOLVERS, "compute_config_file", unavailable)
    response = settings.settings_response(1, {})
    assert _states(response)["compute_config_file"] == "unknown"
    assert _states(response)["database"] == "configured"
    assert len(response["bootstrap"]) == 7
    assert "sentinel-private-diagnostic" not in json.dumps(response)


def test_fallback_ciphertext_survives_same_value_dedicated_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SESSION_SECRET", FALLBACK)
    encoded = settings.encode_changes({"github_app_private_key": INLINE_KEY})

    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", FALLBACK)
    settings.validate_stored_settings(encoded)
    response = settings.settings_response(1, encoded)
    assert response["secret_configured"]["github_app_private_key"] is True

    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", OTHER_ROOT)
    with pytest.raises(SettingsEncryptionError):
        settings.validate_stored_settings(encoded)
    with pytest.raises(SettingsEncryptionError):
        settings.settings_response(1, encoded)


def test_deployment_metadata_keeps_the_legacy_settings_update_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", ROOT)
    encoded = settings.encode_changes(
        {"source_control_encryption_key": "sentinel-user-credential-key-of-32-bytes"}
    )
    settings.validate_settings_update({}, encoded)
    response = settings.settings_response(3, encoded)
    assert response["revision"] == 3
    assert set(response) == {
        "revision",
        "definitions",
        "values",
        "secret_configured",
        "bootstrap",
        "setup",
    }
    assert all(
        value is None
        for key, value in response["values"].items()
        if response["secret_configured"].get(key)
    )
