"""Settings updates require admin access and never disclose credential values."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_settings
from cloud_agent.app import install_admin_no_store
from cloud_agent.source_control import SourceControlError

ADMIN = {"Authorization": "Bearer test-settings-admin"}
SECRET = "private-settings-credential"


class FakeSettings:
    def __init__(self) -> None:
        self.revision = 4
        self.values: dict[str, Any] = {
            "limit": 20,
            "credential": "encrypted-existing",
            "name": "old",
        }
        self.updates: list[dict[str, Any]] = []

    async def read_settings(self) -> tuple[int, dict[str, Any]]:
        return self.revision, deepcopy(self.values)

    async def update_settings(
        self, changes: dict[str, Any]
    ) -> tuple[int, dict[str, Any], dict[str, Any]]:
        previous = deepcopy(self.values)
        self.values.update(changes)
        self.updates.append(changes)
        self.revision += 1
        return self.revision, deepcopy(self.values), previous


class FakeAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_admin_action(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _view(revision: int, encoded: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": revision,
        "definitions": [],
        "values": {**encoded, "credential": None},
        "secret_configured": {"credential": bool(encoded.get("credential"))},
        "bootstrap": [],
    }


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(values)
    for key, value in encoded.items():
        if key not in {"limit", "credential", "name"}:
            raise ValueError("Unknown setting.")
        if key == "limit" and (type(value) is not int or value < 1):
            raise ValueError("Limit must be a positive integer.")
        if key == "credential" and value is not None:
            encoded[key] = "encrypted-replacement"
    return encoded


@pytest.fixture
async def settings_api(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-settings-admin")
    monkeypatch.setattr(admin_settings.settings, "encode_changes", _encode)
    monkeypatch.setattr(admin_settings.settings, "settings_response", _view)
    applied = []
    monkeypatch.setattr(
        admin_settings.settings,
        "apply_settings",
        lambda revision, encoded: applied.append((revision, encoded)),
    )
    repository, audit = FakeSettings(), FakeAudit()
    app = FastAPI()
    app.state.storage = SimpleNamespace(settings=repository, audit=audit)
    app.include_router(admin_settings.router)
    install_admin_no_store(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, repository, audit, app, applied


async def test_settings_reads_and_writes_require_admin(settings_api) -> None:
    client, repository, audit, _, applied = settings_api
    for method in ("GET", "PUT"):
        response = await client.request(
            method, "/admin/agent/settings", json={"settings": {"limit": 7}}
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert repository.updates == audit.calls == applied == []


async def test_settings_read_hides_secrets_and_is_not_cached(settings_api) -> None:
    client, _, audit, _, _ = settings_api
    response = await client.get("/admin/agent/settings", headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["values"]["credential"] is None
    assert response.json()["secret_configured"]["credential"] is True
    assert "encrypted-existing" not in response.text
    assert audit.calls == []


async def test_partial_update_preserves_values_applies_locally_and_audits_safely(
    settings_api,
) -> None:
    client, repository, audit, _, applied = settings_api
    response = await client.put(
        "/admin/agent/settings",
        headers=ADMIN,
        json={"settings": {"limit": 7, "credential": SECRET}},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["values"] == {"limit": 7, "credential": None, "name": "old"}
    assert repository.values["credential"] == "encrypted-replacement"
    assert applied == [(5, repository.values)]
    assert audit.calls[0]["actor_id"] == "admin-token"
    assert audit.calls[0]["action"] == "agent.settings.update"
    assert audit.calls[0]["details"]["changes"] == {
        "limit": {"previous": 20, "value": 7},
        "credential": {"previous": "[redacted]", "value": "[redacted]"},
    }
    assert SECRET not in response.text + repr(audit.calls)
    assert "encrypted-replacement" not in response.text + repr(audit.calls)


async def test_secret_reset_is_recorded_without_exposing_previous_credential(settings_api) -> None:
    client, _, audit, _, _ = settings_api
    response = await client.put(
        "/admin/agent/settings", headers=ADMIN, json={"settings": {"credential": None}}
    )
    assert response.status_code == 200
    assert response.json()["secret_configured"]["credential"] is False
    assert audit.calls[0]["details"]["changes"]["credential"] == {
        "previous": "[redacted]",
        "value": None,
    }


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": True}, {"unknown": "value"}])
async def test_invalid_values_are_rejected_without_a_write(settings_api, values) -> None:
    client, repository, audit, _, applied = settings_api
    response = await client.put("/admin/agent/settings", headers=ADMIN, json={"settings": values})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert repository.updates == audit.calls == applied == []


@pytest.mark.parametrize(
    "body",
    [{"settings": SECRET}, {"settings": {}, "unexpected": SECRET}, [SECRET]],
)
async def test_malformed_bodies_never_echo_submitted_secrets(settings_api, body) -> None:
    client, repository, _, _, _ = settings_api
    response = await client.put("/admin/agent/settings", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text
    assert repository.updates == []


async def test_secret_encryption_failure_is_safe_and_does_not_write(
    settings_api, monkeypatch
) -> None:
    client, repository, audit, _, applied = settings_api

    def unavailable(values):
        raise SourceControlError(SECRET)

    monkeypatch.setattr(admin_settings.settings, "encode_changes", unavailable)
    response = await client.put(
        "/admin/agent/settings", headers=ADMIN, json={"settings": {"credential": SECRET}}
    )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text
    assert repository.updates == audit.calls == applied == []


async def test_settings_require_database(settings_api) -> None:
    client, _, _, app, _ = settings_api
    app.state.storage = None
    response = await client.get("/admin/agent/settings", headers=ADMIN)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("error_type", [SourceControlError, ValueError])
async def test_unreadable_stored_configuration_returns_a_safe_error(
    settings_api, monkeypatch, error_type
) -> None:
    client, _, _, _, _ = settings_api

    def unreadable(revision, encoded):
        raise error_type(SECRET)

    monkeypatch.setattr(admin_settings.settings, "settings_response", unreadable)
    response = await client.get("/admin/agent/settings", headers=ADMIN)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text


async def test_real_registry_encrypts_secrets_and_exposes_only_configured_status(
    monkeypatch,
) -> None:
    settings = admin_settings.settings
    monkeypatch.setenv("ADMIN_TOKEN", "test-settings-admin")
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "settings-test-key-at-least-32-bytes")
    monkeypatch.setattr(settings, "_values", None)
    monkeypatch.setattr(settings, "_revision", -1)
    repository, audit = FakeSettings(), FakeAudit()
    repository.values = {"max_attempts": 3}
    app = FastAPI()
    app.state.storage = SimpleNamespace(settings=repository, audit=audit)
    app.include_router(admin_settings.router)
    install_admin_no_store(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/admin/agent/settings",
            headers=ADMIN,
            json={"settings": {"max_attempts": 2, "github_publish_token": SECRET}},
        )
    assert response.status_code == 200
    assert response.json()["values"]["github_publish_token"] is None
    assert response.json()["secret_configured"]["github_publish_token"] is True
    assert SECRET not in response.text + repr(repository.values) + repr(audit.calls)
    assert set(repository.values["github_publish_token"]) == {"ciphertext"}
    assert settings.text_setting("github_publish_token") == SECRET
    assert settings.integer_setting("max_attempts") == 2
