"""The service keys a new deployment's first settings row generates, and nothing else does.

``initialize_settings`` creates the settings row once. When the environment
supplies no host signing key, user credential key or compute credential key,
that first row gets random ones, so a new deployment starts without an operator
inventing them. An existing row is never given one, empty or not. See
docs/agents/specs/2026-11-04-first-install-keys.zh-CN.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_agent import settings
from cloud_agent.application.model_connections import model_connection_cipher

_KEYS = settings.GENERATED_ON_FIRST_START
_ROOT = "test-settings-root-key-of-at-least-32-bytes"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    settings.reset_settings()
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.delenv("AGENT_COMPUTE_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _ROOT)
    yield
    settings.reset_settings()


class Repository:
    """The settings row, merged as ``seed_settings`` merges it: what is stored wins."""

    def __init__(self, revision: int = -1, values: dict[str, Any] | None = None) -> None:
        self.revision = revision
        self.values: dict[str, Any] = dict(values or {})
        self.seeds = 0
        #: A row another listener stores between this one's read and its seed.
        self.racing: dict[str, Any] | None = None

    async def read_settings(self, *, after_revision=-1):
        return (self.revision, self.values) if self.revision > after_revision else (-1, {})

    async def seed_settings(self, values):
        self.seeds += 1
        if self.racing is not None:
            self.revision, self.values = 1, dict(self.racing)
        if self.revision < 0:
            self.revision, self.values = 1, dict(values)
        else:
            merged = {**values, **self.values}
            if merged != self.values:
                self.revision += 1
                self.values = merged
        return self.revision, self.values


async def test_a_new_deployment_gets_each_service_key_once() -> None:
    repository = Repository()

    await settings.initialize_settings(repository)

    keys = {key: settings.text_setting(key) for key in _KEYS}
    assert all(len(value.encode()) >= 32 for value in keys.values())
    assert len(set(keys.values())) == len(keys)
    assert not any(value in json.dumps(repository.values) for value in keys.values())
    # The startup check that refused a new deployment without the credential key passes.
    model_connection_cipher()
    setup = settings.settings_response(repository.revision, repository.values)["setup"]
    groups = {group["key"]: group for group in setup["groups"]}
    assert groups["credentials"]["configured"] is True
    assert groups["compute"]["configured"] is True
    assert {field["key"] for field in groups["execution"]["missing"]} == {"sandbox_manager_token"}

    # A restart adopts the stored keys and generates nothing.
    settings.reset_settings()
    await settings.initialize_settings(repository)
    assert repository.seeds == 1
    assert {key: settings.text_setting(key) for key in _KEYS} == keys


async def test_keys_the_environment_supplies_are_imported_not_replaced(monkeypatch) -> None:
    supplied = {
        "AGENT_CONTROL_TOKEN_SECRET": "operator-host-signing-key-of-32-bytes",
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": "operator-user-credential-key-of-32b",
        "AGENT_COMPUTE_SECRET_KEY": "operator-compute-credential-key-32b",
    }
    for name, value in supplied.items():
        monkeypatch.setenv(name, value)

    await settings.initialize_settings(Repository())

    for key in _KEYS:
        assert settings.text_setting(key) == supplied[settings.SETTINGS_BY_KEY[key].env]


async def test_an_existing_row_is_never_given_a_key(monkeypatch) -> None:
    """An empty stored key stays empty; the setup status names it instead."""
    row = settings.initial_settings()
    late = {setting.storage_key for setting in settings.MIGRATED_SETTINGS[:3]}
    repository = Repository(revision=7, values={k: v for k, v in row.items() if k not in late})

    await settings.initialize_settings(repository)

    assert repository.seeds == 1, "the missing imports were still taken"
    assert all(settings.text_setting(key) == "" for key in _KEYS)


async def test_without_a_settings_root_nothing_is_generated(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY")
    repository = Repository()

    await settings.initialize_settings(repository)

    assert all(settings.text_setting(key) == "" for key in _KEYS)
    setup = settings.settings_response(repository.revision, repository.values)["setup"]
    assert setup["bootstrap_missing"][0]["key"] == "AGENT_SETTINGS_ENCRYPTION_KEY"


async def test_a_legacy_session_secret_root_also_encrypts_the_generated_keys(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "legacy-settings-root-of-32-bytes-or-more")

    await settings.initialize_settings(Repository())

    assert all(len(settings.text_setting(key)) >= 32 for key in _KEYS)


async def test_a_racing_first_row_keeps_the_keys_it_stored() -> None:
    """Two listeners creating the row at once agree on the one stored first."""
    winner, _generated = settings.first_settings()
    repository = Repository()
    repository.racing = winner

    await settings.initialize_settings(repository)

    stored = {key: settings.text_setting(key) for key in _KEYS}
    settings.apply_settings(99, winner)
    assert stored == {key: settings.text_setting(key) for key in _KEYS}
