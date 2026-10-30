"""Settings registered after a database existed, and their import on upgrade.

P1 section 5.1: eight registrations reached ``SETTINGS`` without
``MIGRATED_SETTINGS``, so a database created before them never imported their
environment value. Two former environment readers (the live-stream ceiling and
the API documentation switch) became settings. These tests pin what an
existing database does when it next starts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cloud_agent import settings
from cloud_agent.api._stream_slots import DEFAULT_MAX_STREAMS_PER_USER, max_streams_per_user
from cloud_agent.app import create_app

_LATE = {
    "WEB_SEARCH_PROVIDER": ("web_search_provider", "brave"),
    "SERPER_API_KEY": ("serper_api_key", "serper-key-example"),
    "BRAVE_SEARCH_API_KEY": ("brave_search_api_key", "brave-key-example"),
    "PARALLEL_API_KEY": ("parallel_api_key", "parallel-key-example"),
    "AGENT_MCP_GITHUB_CLIENT_ID": ("mcp_github_client_id", "Iv1.example"),
    "AGENT_MCP_GITHUB_CLIENT_SECRET": ("mcp_github_client_secret", "mcp-secret-example"),
    "AGENT_WEB_DOCS_URL": ("site.docs_url", "https://docs.example.org"),
    "AGENT_WEB_CONTACT_URL": ("site.contact_url", "mailto:ops@example.org"),
}


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
    def __init__(self, revision: int = -1, values: dict[str, Any] | None = None) -> None:
        self.revision = revision
        self.values: dict[str, Any] = dict(values or {})
        self.seeds = 0

    async def read_settings(self, *, after_revision=-1):
        return (self.revision, self.values) if self.revision > after_revision else (-1, {})

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


def _row_before_late_registrations() -> dict[str, Any]:
    """A complete row as a database created before the eight registrations holds it."""
    row = settings.initial_settings()
    late = {key for key, _ in _LATE.values()} | {"streams.max_per_user", "api_docs"}
    return {key: value for key, value in row.items() if key not in late}


def test_every_registration_with_an_environment_name_is_migrated() -> None:
    migrated = {setting.key for setting in settings.MIGRATED_SETTINGS}
    for _env, (key, _value) in _LATE.items():
        assert key in migrated
    assert {"streams.max_per_user", "api_docs"} <= migrated
    # Moving a definition into the migrated list must not register it twice.
    keys = [setting.key for setting in settings.SETTINGS]
    assert len(keys) == len(set(keys))


async def test_an_existing_row_imports_the_late_registrations_once(monkeypatch) -> None:
    for env, (_key, value) in _LATE.items():
        monkeypatch.setenv(env, value)
    monkeypatch.setenv("AGENT_MAX_STREAMS_PER_USER", "40")
    repository = Repository(7, _row_before_late_registrations())
    await settings.initialize_settings(repository)
    assert repository.revision == 8
    for _env, (key, value) in _LATE.items():
        assert settings.get_setting(key) == value
    assert settings.integer_setting("streams.max_per_user") == 40
    # Secrets are stored encrypted, never as the environment text.
    stored = json.dumps(repository.values)
    for secret in ("serper-key-example", "brave-key-example", "mcp-secret-example"):
        assert secret not in stored
    # Imported once: a later environment change is not reimported.
    monkeypatch.setenv("AGENT_MAX_STREAMS_PER_USER", "99")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "serper")
    settings.reset_settings()
    await settings.initialize_settings(repository)
    assert repository.seeds == 1
    assert settings.integer_setting("streams.max_per_user") == 40
    assert settings.text_setting("web_search_provider") == "brave"


async def test_a_saved_value_is_not_replaced_by_the_late_import(monkeypatch) -> None:
    row = _row_before_late_registrations()
    row["web_search_provider"] = "parallel"
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
    repository = Repository(3, row)
    await settings.initialize_settings(repository)
    assert settings.text_setting("web_search_provider") == "parallel"


async def test_an_invalid_value_on_upgrade_imports_the_default_and_names_the_variable(
    monkeypatch, caplog
) -> None:
    repository = Repository(2, _row_before_late_registrations())
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "not-a-provider")
    monkeypatch.setenv("AGENT_WEB_DOCS_URL", "javascript:alert(1)")
    with caplog.at_level(logging.WARNING):
        await settings.initialize_settings(repository)
    assert settings.text_setting("web_search_provider") == "auto"
    assert settings.text_setting("site.docs_url") == ""
    assert "WEB_SEARCH_PROVIDER" in caplog.text
    assert "not-a-provider" not in caplog.text
    assert "javascript:alert" not in caplog.text


def test_a_new_database_still_refuses_an_invalid_strict_value(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "not-a-provider")
    with pytest.raises(ValueError, match="Web search provider"):
        settings.initial_settings()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("40", 40), ("0", 24), ("-3", 24), ("many", 24), ("", 24)],
)
def test_the_stream_ceiling_imports_what_its_old_reader_accepted(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENT_MAX_STREAMS_PER_USER", raw)
    row = settings.initial_settings()
    settings.apply_settings(1, row)
    assert max_streams_per_user() == expected


def test_the_stream_ceiling_follows_the_saved_value_not_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAX_STREAMS_PER_USER", "40")
    row = settings.initial_settings()
    settings.apply_settings(1, row)
    assert max_streams_per_user() == 40
    row.update(settings.encode_changes({"streams.max_per_user": 10}))
    settings.apply_settings(2, row)
    monkeypatch.setenv("AGENT_MAX_STREAMS_PER_USER", "77")
    assert max_streams_per_user() == 10
    settings.reset_settings()
    monkeypatch.setenv("AGENT_MAX_STREAMS_PER_USER", "nonsense")
    # Before a snapshot, an unusable environment value is still the default.
    assert max_streams_per_user() == DEFAULT_MAX_STREAMS_PER_USER


def test_api_docs_follow_the_saved_setting_without_rebuilding_the_app(monkeypatch) -> None:
    row = settings.initial_settings()
    settings.apply_settings(1, row)
    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404
    row.update(settings.encode_changes({"api_docs": True}))
    settings.apply_settings(2, row)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert "/healthz" in client.get("/openapi.json").json()["paths"]
    # The documentation routes do not describe themselves.
    assert "/openapi.json" not in client.get("/openapi.json").json()["paths"]
    row.update(settings.encode_changes({"api_docs": False}))
    settings.apply_settings(3, row)
    assert client.get("/openapi.json").status_code == 404


def test_new_settings_declare_how_they_take_effect() -> None:
    definitions = {setting.key: setting.definition() for setting in settings.SETTINGS}
    assert definitions["streams.max_per_user"]["apply_mode"] == "next_operation"
    assert definitions["api_docs"]["apply_mode"] == "next_operation"
    assert all("apply_mode" in definition for definition in definitions.values())
