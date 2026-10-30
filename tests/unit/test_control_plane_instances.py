"""Instance reports, restart-required state and the mixed-version gate (WP4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cloud_agent import instances, settings
from cloud_agent.api import admin_management, admin_settings
from cloud_agent.api.deps import get_storage, verify_admin_access


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.reset_settings()
    instances.reset_identity()
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-settings-session-key-32-bytes-long")
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY", raising=False)
    yield
    settings.reset_settings()
    instances.reset_identity()


def _instance(instance_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "instance_id": instance_id,
        "role": "api",
        "machine": "cp-1",
        "build_sha": "a" * 40,
        "capabilities": list(instances.CONTROL_PLANE_CAPABILITIES),
        "adopted_settings_revision": 3,
        "restart_required": [],
        "details": {"root_key_source": "fallback", "root_key_id": "x"},
        "started_at": datetime.now(UTC),
        "last_seen_at": datetime.now(UTC),
        "stopped_at": None,
        "current": True,
    }
    row.update(overrides)
    return row


class FakeInstances:
    def __init__(self, rows: list[dict[str, Any]], sessions: list[dict[str, Any]] | None = None):
        self.rows = rows
        self.sessions = sessions or []
        self.reports: list[dict[str, Any]] = []

    async def list_instances(self):
        return list(self.rows)

    async def unidentified_sessions(self):
        return list(self.sessions)

    async def report(self, **values):
        self.reports.append(values)

    async def stop(self, instance_id):
        self.reports.append({"stopped": instance_id})


def test_the_key_identifier_is_stable_and_one_way() -> None:
    assert instances.key_identifier("") == ""
    first = instances.key_identifier("a-root-key-of-at-least-thirty-two-bytes")
    assert first == instances.key_identifier("a-root-key-of-at-least-thirty-two-bytes")
    assert first != instances.key_identifier("another-root-key-of-thirty-two-bytes!")
    assert "root-key" not in first and len(first) == 16


def test_the_root_key_state_names_its_source(monkeypatch) -> None:
    assert instances.root_key_state()["root_key_source"] == "fallback"
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "dedicated-root-key-of-thirty-two-b")
    monkeypatch.setenv(
        "AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS", "old-root-key-of-thirty-two-bytes!"
    )
    state = instances.root_key_state()
    assert state["root_key_source"] == "dedicated"
    assert state["root_key_previous_id"] == instances.key_identifier(
        "old-root-key-of-thirty-two-bytes!"
    )
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY")
    monkeypatch.delenv("AGENT_SESSION_SECRET")
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS")
    assert instances.root_key_state() == {"root_key_source": "missing", "root_key_id": ""}


def test_every_restart_setting_names_the_roles_that_build_it() -> None:
    restart = {s.key for s in settings.SETTINGS if s.apply_mode == "restart"}
    assert restart == set(instances.RESTART_ROLES)


def test_a_restart_setting_changed_after_startup_is_reported_until_restart() -> None:
    row = settings.initial_settings()
    settings.apply_settings(1, row)
    mcp = instances.RestartTracker("mcp")
    api = instances.RestartTracker("api")
    assert mcp.pending() == []
    row.update(settings.encode_changes({"mcp_allow_plain_http": True}))
    settings.apply_settings(2, row)
    assert mcp.pending() == ["mcp_allow_plain_http"]
    # The API process builds no MCP transport, so it owes no restart.
    assert api.pending() == []
    # A restarted process builds with the new value.
    assert instances.RestartTracker("mcp").pending() == []


def test_the_application_name_identifies_the_process() -> None:
    identity = instances.process_identity("mcp")
    assert identity.application_name == f"cloud-agent:mcp:{identity.instance_id}"
    assert len(identity.application_name.encode()) <= 63


async def test_the_fleet_supports_a_capability_only_on_complete_evidence() -> None:
    ok = FakeInstances([_instance("1" * 32), _instance("2" * 32, role="mcp")])
    verdict = await instances.fleet_supports(ok, "model-connections-v1")
    assert verdict.supported and verdict.reason() == ""

    old = FakeInstances([_instance("1" * 32), _instance("2" * 32, capabilities=["x"])])
    verdict = await instances.fleet_supports(old, "model-connections-v1")
    assert not verdict.supported and "do not support" in verdict.reason()

    stale = FakeInstances(
        [
            _instance("1" * 32),
            _instance(
                "2" * 32, current=False, last_seen_at=datetime.now(UTC) - timedelta(seconds=60)
            ),
        ]
    )
    verdict = await instances.fleet_supports(stale, "model-connections-v1")
    assert not verdict.supported and verdict.stale

    nobody = FakeInstances([])
    assert not (await instances.fleet_supports(nobody, "model-connections-v1")).supported

    legacy = FakeInstances([_instance("1" * 32)], sessions=[{"application_name": ""}])
    verdict = await instances.fleet_supports(legacy, "model-connections-v1")
    assert not verdict.supported and "database session" in verdict.reason()


async def test_reporting_sends_the_adopted_revision_and_stops_cleanly(monkeypatch) -> None:
    import asyncio

    row = settings.initial_settings()
    settings.apply_settings(7, row)
    repo = FakeInstances([])
    monkeypatch.setattr(instances, "REPORT_INTERVAL_S", 0.01)
    task = asyncio.create_task(instances.report_forever(repo, "api"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.reports[0]["adopted_settings_revision"] == 7
    assert repo.reports[0]["capabilities"] == instances.CONTROL_PLANE_CAPABILITIES
    assert "root_key_id" in repo.reports[0]["details"]
    assert repo.reports[-1] == {"stopped": instances.process_identity("api").instance_id}


class FakeSettingsRepo:
    def __init__(self) -> None:
        self.revision = 3
        self.values = settings.initial_settings()

    async def read_settings(self, *, after_revision=-1):
        return self.revision, self.values

    async def update_settings(self, changes, *, expected_revision=None):
        previous = dict(self.values)
        self.values = {**self.values, **changes}
        self.revision += 1
        return self.revision, self.values, previous


class FakeAudit:
    async def log(self, *args, **kwargs):
        return None

    async def log_admin_action(self, *args, **kwargs):
        return None


class FakeHostPool:
    async def list_runner_hosts(self):
        return [
            {
                "host": "runner-a",
                "status": "online",
                "capabilities": ["host-settings-v1"],
                "settings_desired": {"agent_timeout_s": 900},
                "settings_applied": {"agent_timeout_s": 3600},
                "config_reports": {"runtime_registry_url": ""},
            }
        ]


class FakeStorage:
    def __init__(self, instance_rows, sessions=None):
        self.settings = FakeSettingsRepo()
        self.audit = FakeAudit()
        self.instances = FakeInstances(instance_rows, sessions)
        self.host_pool = FakeHostPool()


def _client(storage) -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(admin_settings.router)
    app.include_router(admin_management.router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
    return TestClient(app)


def test_model_connections_cannot_be_turned_on_without_fleet_evidence(monkeypatch) -> None:
    async def no_log(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_settings, "log_admin_action", no_log)
    settings.apply_settings(3, settings.initial_settings())
    storage = FakeStorage([_instance("1" * 32, capabilities=["instance-report-v1"])])
    refused = _client(storage).put(
        "/admin/agent/settings", json={"settings": {"model_connections_enabled": True}}
    )
    assert refused.status_code == 409
    assert "do not support" in refused.json()["detail"]
    assert storage.settings.revision == 3

    storage = FakeStorage([_instance("1" * 32)])
    accepted = _client(storage).put(
        "/admin/agent/settings", json={"settings": {"model_connections_enabled": True}}
    )
    assert accepted.status_code == 200
    assert accepted.json()["values"]["model_connections_enabled"] is True
    # Turning it off is never gated.
    off = _client(storage).put(
        "/admin/agent/settings", json={"settings": {"model_connections_enabled": False}}
    )
    assert off.status_code == 200


def test_the_status_counts_only_current_reports_at_the_saved_revision() -> None:
    rows = [
        _instance("1" * 32, adopted_settings_revision=3),
        _instance("2" * 32, role="mcp", adopted_settings_revision=2),
        _instance("3" * 32, adopted_settings_revision=3, restart_required=["mcp_allow_plain_http"]),
        _instance("4" * 32, current=False, adopted_settings_revision=3),
    ]
    storage = FakeStorage(rows, sessions=[{"application_name": "psql", "usename": "u"}])
    body = _client(storage).get("/admin/agent/management/status").json()
    assert body["settings"]["saved_revision"] == 3
    assert body["settings"]["reporting"] == 3
    assert body["settings"]["adopted"] == 2
    assert body["settings"]["restart_required"] == {"mcp_allow_plain_http": ["api@cp-1"]}
    stale = next(i for i in body["instances"] if i["instance_id"] == "4" * 32)
    assert stale["adopted"] is False
    assert body["unidentified_sessions"][0]["application_name"] == "psql"
    assert body["hosts"][0]["settings_pending"] == ["agent_timeout_s"]
