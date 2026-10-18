"""Tests for the admin agent runner host pool endpoints.

Ported from the gateway with assertions untouched. One piece of wiring changed
and could not not change: the gateway's ``AppServices`` container became
``app.state.storage``. ``verify_admin_access`` is this repository's own and is
left real — the ``ADMIN_TOKEN`` these tests knock with is the door it
actually opens.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import AllCapabilities

from cloud_agent.api import admin_hosts as admin_router
from cloud_agent.api.deps import get_storage

ADMIN = {"Authorization": "Bearer test-admin"}


class _AuditTrail:
    """The audit capability, in the shape ``Storage`` really exposes it.

    The handler records through ``store.audit.record_admin_action``. This fake
    used to carry that method flat on the store *and* an ``audit`` list beside
    it — the shape #210 left behind — which is why these tests stayed green
    while the deployed service wrote no audit rows at all.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record_admin_action(self, **kwargs: Any) -> int:
        self.rows.append(kwargs)
        return len(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]


class FakeHostStore(AllCapabilities):
    """The runner-host capability, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        # The singleton policy row: three facts, in one place, as in the store.
        self.active_host: str | None = None
        self.runtime_versions: dict[str, str] = {}
        self.global_settings: dict[str, Any] = {}
        self.autoscaling_managed: set[str] = set()
        self.archive_settings: dict[str, Any] = {
            "enabled": False,
            "s3_uri": "s3://freeinference/cloud-agent/sandbox",
            "region": "",
            "kms_key_arn": "",
            "expected_bucket_owner": "",
            "max_archive_bytes": 2 * 1024**3,
            "multipart_threshold_bytes": 8 * 1024**2,
            "multipart_part_bytes": 8 * 1024**2,
            "revision": 0,
            "updated_at": datetime.now(timezone.utc),
        }
        self.audit = _AuditTrail()

    def seed(self, host: str, *, active: bool = False, seen_minutes_ago: float = 0.0) -> None:
        seen = datetime.now(timezone.utc) - timedelta(minutes=seen_minutes_ago)
        self.rows[host] = {
            "host": host,
            "last_worker_id": f"w-{host}",
            "first_seen_at": seen,
            "last_seen_at": seen,
            "drain_fence_epoch": uuid4(),
            "runtime_versions": {"claude-code": "2.1.220"},
            "runtime_versions_available": {"claude-code": ["2.1.220", "2.1.235"]},
        }
        if active:
            self.active_host = host

    async def active_runner_host(self) -> str | None:
        return self.active_host

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        rows = sorted(
            ({**row, "is_active": row["host"] == self.active_host} for row in self.rows.values()),
            key=lambda row: row["last_seen_at"],
            reverse=True,
        )
        return rows, self.active_host, dict(self.runtime_versions), datetime.now(timezone.utc)

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        # Mirrors the store: a version no host offers is refused, never stored.
        if version is None:
            self.runtime_versions.pop(runtime, None)
            return True
        offered = any(
            version in ((row.get("runtime_versions_available") or {}).get(runtime) or [])
            for row in self.rows.values()
        )
        if not offered:
            return False
        self.runtime_versions[runtime] = version
        return True

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        # Mirrors the store: an unknown host changes nothing at all.
        if host is not None and host not in self.rows:
            return False
        self.active_host = host
        return True

    async def forget_runner_host(self, *, host: str) -> str:
        # The active-host refusal is the store's, not the caller's.
        if host == self.active_host:
            return "active"
        return "deleted" if self.rows.pop(host, None) is not None else "unknown"

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        row = self.rows.get(host)
        return None if row is None else dict(row)

    async def set_host_slots_desired(self, *, host: str, slots: int | None) -> str:
        # Mirrors the store: the row decides, and any size the schema let
        # through is stored — the host grows into it.
        row = self.rows.get(host)
        if row is None:
            return "unknown"
        if host in self.autoscaling_managed:
            return "autoscaling_managed"
        row["slots_desired"] = slots
        return "ok"

    async def host_settings(self) -> dict[str, Any]:
        return dict(self.global_settings)

    async def set_host_settings(self, *, changes: dict[str, int | None]) -> dict[str, Any]:
        previous = dict(self.global_settings)
        self.global_settings = _changed(previous, changes)
        return previous

    async def sandbox_archive_settings(self) -> dict[str, Any]:
        return dict(self.archive_settings)

    async def set_sandbox_archive_settings(
        self, *, expected_revision: int, settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        if expected_revision > self.archive_settings["revision"]:
            return None
        same = all(self.archive_settings.get(key) == value for key, value in settings.items())
        if same:
            return {**self.archive_settings, "_changed": False}
        if expected_revision != self.archive_settings["revision"]:
            return None
        self.archive_settings = {
            **settings,
            "s3_uri": "s3://freeinference/cloud-agent/sandbox",
            "revision": expected_revision + 1,
            "updated_at": datetime.now(timezone.utc),
        }
        return {**self.archive_settings, "_changed": True}

    async def set_runner_host_settings(
        self, *, host: str, changes: dict[str, int | None]
    ) -> dict[str, Any] | None:
        # Mirrors the store: an unknown host changes nothing and says so.
        row = self.rows.get(host)
        if row is None:
            return None
        previous = dict(row.get("settings_desired") or {})
        row["settings_desired"] = _changed(previous, changes)
        return previous


def _changed(stored: dict[str, Any], changes: dict[str, int | None]) -> dict[str, Any]:
    """Merge an admin's change the way the store does: None clears one key."""
    updated = dict(stored)
    for key, value in changes.items():
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = value
    return updated


def _admin_app(store: FakeHostStore | None) -> FastAPI:
    """Mount the admin router over one store."""
    app = FastAPI()
    app.state.storage = store
    app.include_router(admin_router.router)
    app.dependency_overrides[get_storage] = lambda: store
    return app


@pytest.fixture
async def admin_client(monkeypatch):
    store = FakeHostStore()
    app = _admin_app(store)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-secret")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store


@pytest.mark.asyncio
async def test_pool_is_empty_and_unpinned_before_any_runner_polls(admin_client):
    client, _ = admin_client

    response = await client.get("/admin/agent/runner-hosts", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["hosts"] == []
    # Unpinned, not "pinned to nothing": a deployment that never touches this
    # page must keep claiming exactly as it did before the feature existed.
    assert body["active_host"] is None


@pytest.mark.asyncio
async def test_listing_reports_poll_age_rather_than_liveness(admin_client):
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("runner-b", seen_minutes_ago=45)

    body = (await client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()

    assert body["active_host"] == "runner-a"
    by_host = {row["host"]: row for row in body["hosts"]}
    assert by_host["runner-a"]["active"] is True
    assert by_host["runner-a"]["seconds_since_seen"] < 60
    assert 2600 < by_host["runner-b"]["seconds_since_seen"] < 2800
    assert by_host["runner-b"]["active"] is False


@pytest.mark.asyncio
async def test_switching_host_returns_the_new_pool_state(admin_client):
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("runner-b")

    response = await client.put(
        "/admin/agent/runner-hosts/active", json={"host": "runner-b"}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json()["active_host"] == "runner-b"
    assert store.active_host == "runner-b"


@pytest.mark.asyncio
async def test_unpinning_lets_any_runner_claim_again(admin_client):
    client, store = admin_client
    store.seed("runner-b", active=True)

    response = await client.put(
        "/admin/agent/runner-hosts/active", json={"host": None}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json()["active_host"] is None
    assert store.active_host is None


@pytest.mark.asyncio
async def test_pinning_an_unknown_host_is_refused(admin_client):
    """Regression: a typo here parks the queue with nothing in the logs.

    Pinning to a machine no runner has ever polled from means every job queues
    forever, and the only symptom is silence — so the write is refused rather
    than accepted and discovered later.
    """
    client, store = admin_client
    store.seed("runner-a", active=True)

    response = await client.put(
        "/admin/agent/runner-hosts/active", json={"host": "runner-b-typo"}, headers=ADMIN
    )

    assert response.status_code == 404
    assert "has ever polled" in response.json()["detail"]
    # And the previously active host keeps running jobs.
    assert store.active_host == "runner-a"


@pytest.mark.asyncio
async def test_forgetting_the_active_host_is_refused(admin_client):
    """Removing it would silently unpin — every other machine starts claiming."""
    client, store = admin_client
    store.seed("runner-b", active=True)

    response = await client.delete("/admin/agent/runner-hosts/runner-b", headers=ADMIN)

    assert response.status_code == 409
    assert "runner-b" in store.rows


@pytest.mark.asyncio
async def test_forgetting_a_stale_host_drops_it(admin_client):
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("old-box", seen_minutes_ago=6000)

    response = await client.delete("/admin/agent/runner-hosts/old-box", headers=ADMIN)

    assert response.status_code == 200
    assert [row["host"] for row in response.json()["hosts"]] == ["runner-a"]


@pytest.mark.asyncio
async def test_forgetting_an_unknown_host_is_a_404(admin_client):
    client, _ = admin_client

    response = await client.delete("/admin/agent/runner-hosts/never-existed", headers=ADMIN)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_pool_is_admin_only(admin_client):
    """The list names internal machines, and the switch redirects all agent work."""
    client, _ = admin_client

    assert (await client.get("/admin/agent/runner-hosts")).status_code in (401, 403)
    assert (
        await client.put("/admin/agent/runner-hosts/active", json={"host": None})
    ).status_code in (401, 403)


@pytest.mark.asyncio
async def test_endpoints_report_unconfigured_deployments_as_503(monkeypatch):
    """A deployment without agent jobs answers 'not configured', not 500."""
    app = _admin_app(None)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-secret")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/agent/runner-hosts", headers=ADMIN)

    assert response.status_code == 503


# ── Choosing the harness version ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_pool_reports_which_versions_it_offers_and_which_is_chosen(admin_client):
    """Both halves in one response: the menu per host, the choice pool-wide."""
    client, store = admin_client
    store.seed("runner-a")

    body = (await client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()

    assert body["hosts"][0]["runtime_versions_available"] == {"claude-code": ["2.1.220", "2.1.235"]}
    assert body["hosts"][0]["runtime_versions"] == {"claude-code": "2.1.220"}
    # Nothing selected is the state every deployment starts in, and it must be
    # distinguishable from "selected the default".
    assert body["runtime_versions"] == {}


@pytest.mark.asyncio
async def test_selecting_a_version_the_pool_offers_returns_the_new_state(admin_client):
    client, store = admin_client
    store.seed("runner-a")

    response = await client.put(
        "/admin/agent/runtime-versions",
        json={"runtime": "claude-code", "version": "2.1.235"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["runtime_versions"] == {"claude-code": "2.1.235"}
    assert store.runtime_versions == {"claude-code": "2.1.235"}


@pytest.mark.asyncio
async def test_a_version_no_image_holds_is_refused_rather_than_stored(admin_client):
    """Storing it would put a version on the page that no job ever runs.

    The claim falls back when its host cannot serve a selection, so an
    unservable choice is not an outage — it is worse, a control that silently
    does nothing while reporting success.
    """
    client, store = admin_client
    store.seed("runner-a")

    response = await client.put(
        "/admin/agent/runtime-versions",
        json={"runtime": "claude-code", "version": "9.9.9"},
        headers=ADMIN,
    )

    assert response.status_code == 404
    # The refusal has to say how to make it true, or it is a dead end. An
    # agent Settings can update is downloaded by updating to the version…
    detail = response.json()["detail"]
    assert "Update to it from Settings" in detail
    assert "EXTRA_VERSIONS" not in detail
    # …and only one it cannot, Kilo, has to be built into the image.
    kilo = await client.put(
        "/admin/agent/runtime-versions",
        json={"runtime": "kilo", "version": "9.9.9"},
        headers=ADMIN,
    )
    assert kilo.status_code == 404
    assert "EXTRA_VERSIONS" in kilo.json()["detail"]
    assert store.runtime_versions == {}


@pytest.mark.asyncio
async def test_clearing_a_selection_returns_the_runtime_to_the_image_default(admin_client):
    client, store = admin_client
    store.seed("runner-a")
    store.runtime_versions = {"claude-code": "2.1.235"}

    response = await client.put(
        "/admin/agent/runtime-versions",
        json={"runtime": "claude-code", "version": None},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["runtime_versions"] == {}


@pytest.mark.asyncio
async def test_inventory_reports_the_server_owned_new_chat_policy(admin_client, monkeypatch):
    client, _ = admin_client
    for profile in ("untrusted-kata-v1", "trusted-v1"):
        monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", profile)
        response = await client.get("/admin/agent/runner-hosts", headers=ADMIN)
        assert response.status_code == 200
        assert response.json()["execution_profile"] == profile


@pytest.mark.asyncio
async def test_an_admin_action_is_recorded_against_the_administrator(admin_client):
    """The trail says who forgot the host, not only from which address."""
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("old-box", seen_minutes_ago=6000)

    response = await client.delete("/admin/agent/runner-hosts/old-box", headers=ADMIN)

    assert response.status_code == 200
    assert len(store.audit) == 1
    entry = store.audit[0]
    assert entry["action"] == "agent.runner_host.forget"
    assert isinstance(entry["actor_id"], str) and entry["actor_id"]
    assert entry["details"] == {"host": "old-box"}


# ── Requested executor count ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sizing_a_host_returns_the_pool_with_the_requested_count(admin_client):
    client, store = admin_client
    store.seed("runner-a")
    store.rows["runner-a"].update(slots_total=3, slots_used=1)

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/slots", json={"slots": 8}, headers=ADMIN
    )

    assert response.status_code == 200
    row = next(item for item in response.json()["hosts"] if item["host"] == "runner-a")
    assert row["slots_desired"] == 8
    # The reported count is untouched: the heartbeat owns it, and the page
    # shows the two side by side while the host grows into the request.
    assert row["slots_total"] == 3
    assert store.rows["runner-a"]["slots_desired"] == 8


@pytest.mark.asyncio
async def test_clearing_the_request_returns_the_host_to_its_default(admin_client):
    client, store = admin_client
    store.seed("runner-a")
    store.rows["runner-a"].update(slots_total=4, slots_desired=2)

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/slots", json={"slots": None}, headers=ADMIN
    )

    assert response.status_code == 200
    row = next(item for item in response.json()["hosts"] if item["host"] == "runner-a")
    assert row["slots_desired"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("slots", [1, 0, 129, "three"])
async def test_a_size_outside_the_pools_bounds_is_refused_before_the_store_is_asked(
    admin_client, slots
):
    """One executor would run a fork's sibling behind its parent; 129 is a typo."""
    client, store = admin_client
    store.seed("runner-a")
    store.rows["runner-a"].update(slots_total=3)

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/slots", json={"slots": slots}, headers=ADMIN
    )

    assert response.status_code == 422
    assert store.rows["runner-a"].get("slots_desired") is None


@pytest.mark.asyncio
async def test_sizing_an_unknown_host_is_a_404(admin_client):
    client, _ = admin_client

    response = await client.put(
        "/admin/agent/runner-hosts/nope/slots", json={"slots": 2}, headers=ADMIN
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_sizing_an_autoscaling_managed_host_is_refused(admin_client):
    client, store = admin_client
    store.seed("runner-a")
    store.rows["runner-a"]["slots_desired"] = 6
    store.autoscaling_managed.add("runner-a")

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/slots", json={"slots": 8}, headers=ADMIN
    )

    assert response.status_code == 409
    assert "managed by CloudLab autoscaling" in response.json()["detail"]
    assert store.rows["runner-a"]["slots_desired"] == 6
    assert len(store.audit) == 0


@pytest.mark.asyncio
async def test_a_size_change_is_audited_with_both_values(admin_client):
    """The trail answers 'who resized this host, from what' without a diff."""
    client, store = admin_client
    store.seed("runner-a")
    store.rows["runner-a"].update(slots_total=4, slots_desired=3)

    await client.put("/admin/agent/runner-hosts/runner-a/slots", json={"slots": 6}, headers=ADMIN)

    entry = store.audit[-1]
    assert entry["action"] == "agent.runner_host.set_slots"
    assert isinstance(entry["actor_id"], str) and entry["actor_id"]
    assert entry["details"] == {
        "host": "runner-a",
        "old_slots": 3,
        "new_slots": 6,
        "reported": 4,
    }


@pytest.mark.asyncio
async def test_the_pool_lists_every_setting_an_admin_may_set_with_its_bounds(admin_client):
    """The page renders the settings from this list, so it cannot drift from the server's."""
    client, _ = admin_client

    body = (await client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()

    assert body["host_settings"] == {}
    definitions = {item["key"]: item for item in body["host_setting_definitions"]}
    assert set(definitions) == {
        "sandbox_cpu_millicores",
        "sandbox_memory_mib",
        "sandbox_disk_mib",
        "agent_timeout_s",
        "chat_idle_timeout_s",
        "source_import_max_mib",
        "storage_min_available_mib",
        "shared_resident_capacity",
        "terminal_idle_ttl_s",
        "terminal_hard_ttl_s",
        "terminal_max_sessions",
        "terminal_max_sessions_per_workspace",
        "workspace_command_timeout_s",
        "workspace_file_write_max_mib",
        "workspace_file_preview_max_kib",
        "workspace_command_output_max_mib",
        "workspace_directory_max_entries",
        "workspace_download_max_mib",
        "terminal_buffer_max_mib",
        "terminal_buffer_max_events",
        "terminal_output_max_mib",
        "terminal_input_max_kib",
        "terminal_max_streams_per_session",
    }
    assert definitions["source_import_max_mib"] | {"label": None, "description": None} == {
        "key": "source_import_max_mib",
        "env": "AGENT_SOURCE_IMPORT_MAX_MIB",
        "label": None,
        "description": None,
        "unit": "MiB",
        "default": 1024,
        "minimum": 1,
        "maximum": 65536,
        "scope": "runner",
    }
    assert definitions["storage_min_available_mib"] | {"label": None, "description": None} == {
        "key": "storage_min_available_mib",
        "env": "AGENT_STORAGE_MIN_AVAILABLE_MIB",
        "label": None,
        "description": None,
        "unit": "MiB",
        "default": 1024,
        "minimum": 1,
        "maximum": 1048576,
        "scope": "runner",
    }


@pytest.mark.asyncio
async def test_container_sizes_can_be_set_globally_overridden_and_reset(admin_client):
    client, store = admin_client
    store.seed("runner-a")
    sizes = {
        "sandbox_cpu_millicores": 1250,
        "sandbox_memory_mib": 8192,
        "sandbox_disk_mib": 20480,
    }
    response = await client.put(
        "/admin/agent/host-settings", json={"settings": sizes}, headers=ADMIN
    )
    assert response.status_code == 200
    assert response.json()["host_settings"] == sizes

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/settings",
        json={"settings": {"sandbox_cpu_millicores": 500, "sandbox_disk_mib": 0}},
        headers=ADMIN,
    )
    assert response.status_code == 200
    assert response.json()["hosts"][0]["settings_desired"] == {
        "sandbox_cpu_millicores": 500,
        "sandbox_disk_mib": 0,
    }

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/settings",
        json={"settings": {"sandbox_cpu_millicores": None, "sandbox_disk_mib": None}},
        headers=ADMIN,
    )
    assert response.status_code == 200
    assert response.json()["hosts"][0]["settings_desired"] == {}
    assert response.json()["host_settings"] == sizes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"sandbox_cpu_millicores": 99},
        {"sandbox_cpu_millicores": 256001},
        {"sandbox_cpu_millicores": 1250.5},
        {"sandbox_memory_mib": 127},
        {"sandbox_memory_mib": 1048577},
        {"sandbox_disk_mib": -1},
        {"sandbox_disk_mib": 16777217},
        {"sandbox_disk_mib": True},
    ],
)
async def test_invalid_container_sizes_are_refused_without_saving(admin_client, change):
    client, store = admin_client
    response = await client.put(
        "/admin/agent/host-settings", json={"settings": change}, headers=ADMIN
    )
    assert response.status_code == 422
    assert store.global_settings == {}
    assert len(store.audit) == 0


@pytest.mark.asyncio
async def test_a_global_setting_is_saved_changed_and_cleared_one_key_at_a_time(admin_client):
    client, store = admin_client

    response = await client.put(
        "/admin/agent/host-settings",
        json={"settings": {"agent_timeout_s": 7200, "terminal_max_sessions": 32}},
        headers=ADMIN,
    )
    assert response.status_code == 200
    assert response.json()["host_settings"] == {
        "agent_timeout_s": 7200,
        "terminal_max_sessions": 32,
    }

    response = await client.put(
        "/admin/agent/host-settings", json={"settings": {"agent_timeout_s": None}}, headers=ADMIN
    )
    assert response.json()["host_settings"] == {"terminal_max_sessions": 32}
    assert store.global_settings == {"terminal_max_sessions": 32}


def _archive_policy(*, revision: int = 0, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "enabled": True,
        "region": "us-east-1",
        "kms_key_arn": (
            "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
        ),
        "expected_bucket_owner": "123456789012",
        "max_archive_bytes": 2 * 1024**3,
        "multipart_threshold_bytes": 8 * 1024**2,
        "multipart_part_bytes": 8 * 1024**2,
        "revision": revision,
    }
    values.update(changes)
    return values


@pytest.mark.asyncio
async def test_archive_policy_is_read_saved_and_audited_atomically(admin_client):
    client, store = admin_client

    before = await client.get("/admin/agent/sandbox-archive-settings", headers=ADMIN)
    assert before.status_code == 200
    assert before.json()["enabled"] is False
    assert before.json()["s3_uri"] == "s3://freeinference/cloud-agent/sandbox"

    response = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(),
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json() | {"updated_at": None} == {
        **_archive_policy(revision=1),
        "s3_uri": "s3://freeinference/cloud-agent/sandbox",
        "aws_access_key_id": "",
        "aws_secret_access_key_configured": False,
        "updated_at": None,
        "checkpoints_enabled": True,
    }
    assert "aws_secret_access_key" not in response.json()
    assert store.audit[-1]["action"] == "agent.sandbox_archive_settings.set"
    assert store.audit[-1]["details"] == {
        "old_revision": 0,
        "new_revision": 1,
        "old_settings": {
            "enabled": False,
            "region": "",
            "kms_key_arn": "",
            "expected_bucket_owner": "",
            "aws_access_key_id": "",
            "aws_secret_access_key_configured": False,
            "max_archive_bytes": 2 * 1024**3,
            "multipart_threshold_bytes": 8 * 1024**2,
            "multipart_part_bytes": 8 * 1024**2,
            "checkpoints_enabled": False,
        },
        "new_settings": {
            key: value for key, value in _archive_policy().items() if key != "revision"
        }
        | {
            "aws_access_key_id": "",
            "aws_secret_access_key_configured": False,
            "checkpoints_enabled": True,
        },
    }

    no_op = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(revision=1),
        headers=ADMIN,
    )
    retry = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(revision=0),
        headers=ADMIN,
    )
    future = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(revision=99),
        headers=ADMIN,
    )
    assert no_op.status_code == retry.status_code == 200
    assert no_op.json()["revision"] == retry.json()["revision"] == 1
    assert future.status_code == 409
    assert len(store.audit) == 1


@pytest.mark.asyncio
async def test_archive_policy_missing_singleton_timestamp_is_still_readable(admin_client):
    client, store = admin_client
    store.archive_settings["updated_at"] = None

    response = await client.get("/admin/agent/sandbox-archive-settings", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["updated_at"] is None


@pytest.mark.asyncio
async def test_archive_policy_rejects_stale_or_unsafe_writes(admin_client):
    client, store = admin_client
    first = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(),
        headers=ADMIN,
    )
    assert first.status_code == 200

    stale = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(region="us-west-2"),
        headers=ADMIN,
    )
    assert stale.status_code == 422  # KMS region mismatch is rejected before the CAS.

    stale = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(expected_bucket_owner="999999999999"),
        headers=ADMIN,
    )
    assert stale.status_code == 422

    stale = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(enabled=False, region=""),
        headers=ADMIN,
    )
    assert stale.status_code == 409
    assert store.archive_settings["revision"] == 1

    injected = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json={**_archive_policy(revision=1), "aws_access_key_id": "must-not-be-stored"},
        headers=ADMIN,
    )
    assert injected.status_code == 422
    redirected = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json={**_archive_policy(revision=1), "s3_uri": "s3://attacker-bucket/elsewhere"},
        headers=ADMIN,
    )
    assert redirected.status_code == 422
    bool_enabled = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json={**_archive_policy(revision=1), "enabled": 1},
        headers=ADMIN,
    )
    assert bool_enabled.status_code == 422
    assert len(store.audit) == 1


@pytest.mark.asyncio
async def test_archive_policy_stores_an_access_key_without_returning_the_secret(admin_client):
    client, store = admin_client
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    access_key = "AKIAIOSFODNN7EXAMPLE"
    saved = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(
            kms_key_arn="",
            expected_bucket_owner="",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret,
        ),
        headers=ADMIN,
    )

    assert saved.status_code == 200
    body = saved.json()
    assert body["aws_access_key_id"] == access_key
    assert body["aws_secret_access_key_configured"] is True
    assert "aws_secret_access_key" not in body
    assert body["kms_key_arn"] == ""
    assert body["expected_bucket_owner"] == ""
    assert store.archive_settings["aws_secret_access_key"] == secret
    assert secret not in json.dumps(store.audit[-1])

    kept = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(
            revision=1,
            kms_key_arn="",
            expected_bucket_owner="",
            aws_access_key_id=access_key,
            aws_secret_access_key="",
        ),
        headers=ADMIN,
    )
    assert kept.status_code == 200
    assert kept.json()["revision"] == 1
    assert store.archive_settings["aws_secret_access_key"] == secret


@pytest.mark.asyncio
async def test_archive_policy_null_optional_limits_restore_safe_defaults(admin_client):
    client, _ = admin_client

    response = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json={
            **_archive_policy(),
            "max_archive_bytes": None,
            "multipart_threshold_bytes": None,
            "multipart_part_bytes": None,
        },
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["max_archive_bytes"] == 2 * 1024**3
    assert response.json()["multipart_threshold_bytes"] == 8 * 1024**2
    assert response.json()["multipart_part_bytes"] == 8 * 1024**2


@pytest.mark.asyncio
async def test_archive_policy_routes_require_admin_access(admin_client):
    client, _ = admin_client

    assert (await client.get("/admin/agent/sandbox-archive-settings")).status_code in (401, 403)
    assert (
        await client.put("/admin/agent/sandbox-archive-settings", json=_archive_policy())
    ).status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"agent_timeout_s": 59},
        {"shared_resident_capacity": 129},
        {"no_such_setting": 1},
        {"terminal_max_sessions": True},
        {"terminal_max_sessions": "32"},
        {},
    ],
)
async def test_a_setting_outside_its_bounds_is_refused_and_nothing_is_stored(
    admin_client, settings
):
    client, store = admin_client
    store.seed("runner-a")

    for path in ("/admin/agent/host-settings", "/admin/agent/runner-hosts/runner-a/settings"):
        response = await client.put(path, json={"settings": settings}, headers=ADMIN)
        assert response.status_code == 422

    assert store.global_settings == {}
    assert not store.rows["runner-a"].get("settings_desired")
    assert len(store.audit) == 0


@pytest.mark.asyncio
async def test_a_server_setting_is_kept_on_its_row_and_reported_beside_the_hosts_own(
    admin_client,
):
    client, store = admin_client
    store.seed("runner-a")
    store.rows["runner-a"].update(
        settings_applied={"source_import_max_mib": 1024},
        settings_local={"source_import_max_mib": 1024},
    )

    response = await client.put(
        "/admin/agent/runner-hosts/runner-a/settings",
        json={"settings": {"source_import_max_mib": 4096}},
        headers=ADMIN,
    )

    assert response.status_code == 200
    (row,) = response.json()["hosts"]
    # Saved, not yet applied: the host still reports the value it runs with.
    assert row["settings_desired"] == {"source_import_max_mib": 4096}
    assert row["settings_applied"] == {"source_import_max_mib": 1024}
    assert row["settings_local"] == {"source_import_max_mib": 1024}


@pytest.mark.asyncio
async def test_setting_an_unknown_server_is_a_404(admin_client):
    client, _ = admin_client

    response = await client.put(
        "/admin/agent/runner-hosts/nope/settings",
        json={"settings": {"agent_timeout_s": 900}},
        headers=ADMIN,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_value_no_host_could_apply_is_not_shown_as_set(admin_client):
    """What the page shows is what the heartbeat would send, nothing more."""
    client, store = admin_client
    store.seed("runner-a")
    store.global_settings = {"terminal_max_sessions": 0, "retired": 3, "agent_timeout_s": 900}
    store.rows["runner-a"]["settings_desired"] = {"agent_timeout_s": 10**9}

    body = (await client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()

    assert body["host_settings"] == {"agent_timeout_s": 900}
    assert body["hosts"][0]["settings_desired"] == {}


@pytest.mark.asyncio
async def test_setting_changes_are_audited_with_the_old_and_new_values(admin_client):
    client, store = admin_client
    store.seed("runner-a")
    store.global_settings = {"agent_timeout_s": 7200}
    store.rows["runner-a"]["settings_desired"] = {"terminal_max_sessions": 8}

    await client.put(
        "/admin/agent/host-settings",
        json={"settings": {"agent_timeout_s": 900, "chat_idle_timeout_s": 600}},
        headers=ADMIN,
    )
    await client.put(
        "/admin/agent/runner-hosts/runner-a/settings",
        json={"settings": {"terminal_max_sessions": None}},
        headers=ADMIN,
    )

    global_entry, server_entry = store.audit[-2], store.audit[-1]
    assert global_entry["action"] == "agent.host_settings.set"
    assert global_entry["details"] == {
        "changes": {"agent_timeout_s": 900, "chat_idle_timeout_s": 600},
        "old_values": {"agent_timeout_s": 7200, "chat_idle_timeout_s": None},
    }
    assert server_entry["action"] == "agent.runner_host.set_settings"
    assert server_entry["details"] == {
        "host": "runner-a",
        "changes": {"terminal_max_sessions": None},
        "old_values": {"terminal_max_sessions": 8},
    }


@pytest.mark.asyncio
async def test_forgetting_resource_bound_host_returns_conflict_without_mutation(
    admin_client, monkeypatch
):
    client, store = admin_client
    store.seed("bound-host")

    async def refuse(*, host):
        assert host == "bound-host"
        return "resource_bound"

    monkeypatch.setattr(store, "forget_runner_host", refuse)
    response = await client.delete("/admin/agent/runner-hosts/bound-host", headers=ADMIN)
    assert response.status_code == 409
    assert "resource bound" in response.json()["detail"]
    assert "bound-host" in store.rows


@pytest.mark.asyncio
async def test_global_placement_reports_known_compute_admission_refusal(admin_client, monkeypatch):
    client, store = admin_client
    store.seed("held-host")

    async def refuse(*, host):
        assert host == "held-host"
        return False

    monkeypatch.setattr(store, "set_active_runner_host", refuse)
    response = await client.put(
        "/admin/agent/runner-hosts/active", headers=ADMIN, json={"host": "held-host"}
    )
    assert response.status_code == 409
    assert "compute resource" in response.json()["detail"]
    assert store.active_host is None


@pytest.mark.asyncio
async def test_checkpoints_follow_archives_unless_an_admin_opts_out(admin_client):
    client, store = admin_client

    default = await client.put(
        "/admin/agent/sandbox-archive-settings", json=_archive_policy(), headers=ADMIN
    )
    opted_out = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(revision=1) | {"checkpoints_enabled": False},
        headers=ADMIN,
    )

    assert default.status_code == opted_out.status_code == 200
    assert default.json()["checkpoints_enabled"] is True
    assert opted_out.json()["checkpoints_enabled"] is False
    assert store.audit[-1]["details"]["old_settings"]["checkpoints_enabled"] is True
    assert store.audit[-1]["details"]["new_settings"]["checkpoints_enabled"] is False
    read_back = await client.get("/admin/agent/sandbox-archive-settings", headers=ADMIN)
    assert read_back.json()["checkpoints_enabled"] is False

    disabled = await client.put(
        "/admin/agent/sandbox-archive-settings",
        json=_archive_policy(
            revision=2, enabled=False, region="", kms_key_arn="", expected_bucket_owner=""
        )
        | {"checkpoints_enabled": True},
        headers=ADMIN,
    )
    assert disabled.status_code == 200
    assert disabled.json()["checkpoints_enabled"] is False
