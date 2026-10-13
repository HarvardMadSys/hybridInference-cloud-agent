"""Preactivation imports legacy settings without replacing saved configuration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_agent import deploy_readiness, settings
from cloud_agent.storage import assembly


class _SettingsRepository:
    def __init__(self, *, revision: int = -1, values: dict[str, Any] | None = None) -> None:
        self.revision = revision
        self.values = dict(values or {})
        self.seeds: list[dict[str, Any]] = []

    async def read_settings(self) -> tuple[int, dict[str, Any]]:
        return self.revision, self.values

    async def seed_settings(self, values: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.seeds.append(dict(values))
        merged = {**values, **self.values}
        if self.revision < 0 or merged != self.values:
            self.revision = max(1, self.revision + 1)
            self.values = merged
        return self.revision, self.values


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    settings.reset_settings()
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.delenv("AGENT_COMPUTE_CONFIG", raising=False)
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "readiness-test-encryption-key-32-bytes")
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://readiness-test/agent")
    yield
    settings.reset_settings()


async def _run_readiness(
    monkeypatch: pytest.MonkeyPatch,
    repository: _SettingsRepository,
    *,
    expected_token: str,
) -> None:
    pool = SimpleNamespace(close=AsyncMock())
    host_pool = object()
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr(deploy_readiness.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(
        assembly,
        "Storage",
        lambda _: SimpleNamespace(settings=repository, host_pool=host_pool),
    )
    # No new API exists to seed settings during preactivation. The old
    # seed=False path must fail immediately rather than hiding a 30s deadlock.
    monkeypatch.setattr(
        settings.asyncio,
        "sleep",
        AsyncMock(side_effect=AssertionError("Readiness must not wait for a settings writer")),
    )

    async def probe(store, *, sandbox_manager_token, expected_deployment_sha, not_before):
        assert store is host_pool
        assert sandbox_manager_token == expected_token
        assert settings.setting_env("AGENT_SANDBOX_MANAGER_TOKEN") == expected_token
        assert expected_deployment_sha == "target-sha"
        assert not_before.tzinfo is not None
        return deploy_readiness.SharedLiveReadiness(
            True, "target host and sandbox manager are ready"
        )

    readiness = AsyncMock(side_effect=probe)
    monkeypatch.setattr(deploy_readiness, "shared_live_readiness", readiness)
    assert await deploy_readiness.wait_for_shared_live(
        timeout_seconds=10,
        interval_seconds=1,
        expected_deployment_sha="target-sha",
    )
    readiness.assert_awaited_once()
    pool.close.assert_awaited_once()
    create_pool.assert_awaited_once_with(
        "postgresql://readiness-test/agent", min_size=1, max_size=1
    )


async def test_preactivation_bootstraps_settings_before_the_new_api_starts(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "legacy-sandbox-manager-token")
    monkeypatch.setenv("GATEWAY_GRANT_DISPATCH_TOKEN", "legacy-gateway-token")
    monkeypatch.setenv("AGENT_PUBLISH_BASE_BRANCH", "legacy-branch")
    repository = _SettingsRepository()

    await _run_readiness(monkeypatch, repository, expected_token="legacy-sandbox-manager-token")

    assert len(repository.seeds) == 1
    assert repository.revision == 1
    assert all(
        definition.storage_key in repository.values for definition in settings.MIGRATED_SETTINGS
    )
    assert settings.setting_env("GATEWAY_GRANT_DISPATCH_TOKEN") == "legacy-gateway-token"
    assert settings.text_setting("publish_base_branch") == "legacy-branch"
    assert repository.values["workspace_broker_token"] != "legacy-sandbox-manager-token"


async def test_preactivation_preserves_complete_saved_settings_over_stale_env(monkeypatch):
    values = settings.initial_settings()
    values.update(
        settings.encode_changes(
            {
                "sandbox_manager_token": "saved-sandbox-manager-token",
                "publish_base_branch": "saved-branch",
            }
        )
    )
    repository = _SettingsRepository(revision=7, values=values)
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "stale-sandbox-manager-token")
    monkeypatch.setenv("AGENT_PUBLISH_BASE_BRANCH", "stale-branch")

    await _run_readiness(monkeypatch, repository, expected_token="saved-sandbox-manager-token")

    assert repository.seeds == []
    assert repository.revision == 7
    assert repository.values == values
    assert settings.text_setting("publish_base_branch") == "saved-branch"


async def test_preactivation_imports_missing_fields_without_replacing_saved_values(monkeypatch):
    saved = settings.encode_changes(
        {
            "sandbox_manager_token": "saved-sandbox-manager-token",
            "publish_base_branch": "saved-branch",
        }
    )
    migrated = {definition.storage_key for definition in settings.MIGRATED_SETTINGS}
    legacy = {
        key: value for key, value in settings.initial_settings().items() if key not in migrated
    }
    repository = _SettingsRepository(revision=7, values={**legacy, **saved})
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "stale-sandbox-manager-token")
    monkeypatch.setenv("AGENT_PUBLISH_BASE_BRANCH", "stale-branch")
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "newly-imported-control-token-of-32-bytes")

    await _run_readiness(monkeypatch, repository, expected_token="saved-sandbox-manager-token")

    assert len(repository.seeds) == 1
    assert not repository.seeds[0].keys() & saved.keys()
    assert repository.revision == 8
    assert repository.values.items() >= saved.items()
    assert all(
        definition.storage_key in repository.values for definition in settings.MIGRATED_SETTINGS
    )
    assert settings.text_setting("publish_base_branch") == "saved-branch"
    assert (
        settings.setting_env("AGENT_CONTROL_TOKEN_SECRET")
        == "newly-imported-control-token-of-32-bytes"
    )
