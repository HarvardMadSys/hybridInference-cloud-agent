"""The admin endpoints behind the Settings page's Update button."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_runtime_upgrades
from cloud_agent.api.deps import get_storage, verify_admin_access
from cloud_agent.runtime_upgrades import RegistryClient

LATEST = {
    "@anthropic-ai/claude-code": "2.1.300",
    "@openai/codex": "0.162.1",
    "opencode-ai": "1.19.0",
    "@earendil-works/pi-coding-agent": "1.2.0",
    # Where pi used to publish; the page no longer reads it.
    "@mariozechner/pi-coding-agent": "0.73.1",
}
PUBLISHED = {("@anthropic-ai/claude-code", "2.1.300"), ("@openai/codex", "0.162.1")}
MOVED = "please use @earendil-works/pi-coding-agent instead going forward"
DOCUMENTS = {
    "@anthropic-ai/claude-code": {
        "dist-tags": {"latest": "2.1.300"},
        "versions": {"2.1.220": {}, "2.1.300": {}, "2.1.250": {}, "2.2.0-beta.1": {}},
    },
    "@earendil-works/pi-coding-agent": {
        "dist-tags": {"latest": "1.2.0"},
        "versions": {"0.74.0": {}, "1.2.0": {}, "1.1.0": {}},
    },
    "@mariozechner/pi-coding-agent": {
        "dist-tags": {"latest": "0.73.1"},
        "versions": {"0.73.1": {"deprecated": MOVED}, "0.72.1": {"deprecated": MOVED}},
    },
}


class FakeHostPool:
    def __init__(self) -> None:
        self.upgrades: dict[str, dict[str, Any]] = {}
        self.selection: dict[str, str] = {}
        self.hosts = [
            {
                "host": "runner-a",
                "status": "online",
                "enrolled": True,
                "runtime_versions": {
                    "claude-code": "2.1.220",
                    "codex": "0.145.0",
                    "opencode": "1.18.9",
                    "pi": "0.73.1",
                },
                "runtime_versions_available": {"claude-code": ["2.1.220"]},
                "runtime_downloads": [],
            }
        ]
        self.requests: list[dict[str, str]] = []

    async def runtime_upgrade_snapshot(self):
        return dict(self.upgrades), dict(self.selection), list(self.hosts)

    async def request_runtime_upgrade(self, *, runtime: str, version: str, requested_by: str):
        self.requests.append({"runtime": runtime, "version": version, "requested_by": requested_by})
        target = {
            "version": version,
            "before": "2026-10-15T00:00:00Z",
            "nonce": "0123456789abcdef0123456789abcdef",
            "requested_by": requested_by,
            "requested_at": "2026-10-15T00:00:00+00:00",
            "previous_version": self.selection.get(runtime),
            "activated_at": None,
        }
        self.upgrades[runtime] = target
        return target


class FakeStore:
    def __init__(self) -> None:
        self.host_pool = FakeHostPool()
        self.audit = object()


def _registry(*, reachable: bool = True) -> RegistryClient:
    def answer(request: httpx.Request) -> httpx.Response:
        if not reachable:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path.lstrip("/")
        if path in DOCUMENTS:
            return httpx.Response(200, json=DOCUMENTS[path])
        package, _, version = path.rpartition("/")
        if version == "latest":
            return httpx.Response(200, json={"version": LATEST[package]})
        return httpx.Response(200 if (package, version) in PUBLISHED else 404, json={})

    return RegistryClient(base_url="https://registry.test", transport=httpx.MockTransport(answer))


@pytest_asyncio.fixture()
async def api(monkeypatch):
    audited: list[tuple[str, dict[str, Any]]] = []

    async def record(_store, _ip, action, _target, details, **_kwargs):
        audited.append((action, details))

    monkeypatch.setattr(admin_runtime_upgrades, "log_admin_action", record)
    monkeypatch.setattr(admin_runtime_upgrades, "get_client_ip", lambda _request: "127.0.0.1")
    store = FakeStore()
    registry = {"client": _registry()}
    app = FastAPI()
    app.include_router(admin_runtime_upgrades.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
    app.dependency_overrides[admin_runtime_upgrades.get_registry] = lambda: registry["client"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, audited, registry


def _by_runtime(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["runtime"]: item for item in body["runtimes"]}


async def test_each_updatable_agent_is_listed_beside_its_latest_release(api):
    client, _store, _audited, _registry = api

    response = await client.get("/admin/agent/runtime-upgrades")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = _by_runtime(response.json())
    assert sorted(items) == ["claude-code", "codex", "opencode", "pi"]
    assert items["claude-code"] == {
        "runtime": "claude-code",
        "current_version": "2.1.220",
        "default_version": "2.1.220",
        "latest_version": "2.1.300",
        "latest_error": None,
        "update_available": True,
        "upgrade": None,
    }
    # pi's image default is the last release of the package pi left; the
    # update is the latest of the one it moved to.
    assert (items["pi"]["current_version"], items["pi"]["latest_version"]) == ("0.73.1", "1.2.0")
    assert items["pi"]["update_available"] is True


async def test_a_chosen_version_shows_only_once_a_host_cannot_download_it(api):
    """Roll back keeps the version on every host; the page hears of it on failure."""
    client, store, _audited, _registry = api
    store.host_pool.selection = {"claude-code": "2.1.235"}
    store.host_pool.upgrades["claude-code"] = {
        "kind": "selection",
        "version": "2.1.235",
        "before": "2026-10-01T00:00:00Z",
        "nonce": "0123456789abcdef0123456789abcdef",
        "previous_version": None,
        "activated_at": "2026-10-15T00:05:00+00:00",
    }

    quiet = _by_runtime((await client.get("/admin/agent/runtime-upgrades")).json())
    assert quiet["claude-code"]["upgrade"] is None
    assert quiet["claude-code"]["current_version"] == "2.1.235"
    assert quiet["claude-code"]["update_available"] is True

    store.host_pool.hosts[0]["runtime_downloads"] = [
        {
            "runtime": "claude-code",
            "version": "2.1.235",
            "nonce": "0123456789abcdef0123456789abcdef",
            "state": "failed",
            "progress": 0,
            "error": "npm could not reach the registry",
        }
    ]
    failed = _by_runtime((await client.get("/admin/agent/runtime-upgrades")).json())
    upgrade = failed["claude-code"]["upgrade"]
    assert (upgrade["kind"], upgrade["state"], upgrade["version"]) == (
        "selection",
        "failed",
        "2.1.235",
    )
    assert upgrade["error"] == "runner-a: npm could not reach the registry"


async def test_updating_records_a_target_audits_it_and_shows_it_downloading(api):
    client, store, audited, _registry = api

    response = await client.post(
        "/admin/agent/runtime-upgrades", json={"runtime": "claude-code", "version": "2.1.300"}
    )

    assert response.status_code == 200
    assert store.host_pool.requests == [
        {"runtime": "claude-code", "version": "2.1.300", "requested_by": "admin@test"}
    ]
    assert audited == [
        (
            "agent.runtime_version.upgrade",
            {"runtime": "claude-code", "version": "2.1.300", "previous_version": None},
        )
    ]
    item = _by_runtime(response.json())["claude-code"]
    assert item["upgrade"]["state"] == "downloading"
    assert item["upgrade"]["progress"] == 0
    # Not offered again while that version is on its way.
    assert item["update_available"] is False


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"runtime": "kilo", "version": "7.8.8"}, 422),
        ({"runtime": "claude-code", "version": "latest"}, 422),
        ({"runtime": "claude-code", "version": "^2.1.300"}, 422),
        ({"runtime": "claude-code", "version": "9.9.9"}, 422),
    ],
)
async def test_only_a_published_exact_version_of_an_updatable_agent_is_accepted(
    api, payload, status
):
    client, store, audited, _registry = api

    response = await client.post("/admin/agent/runtime-upgrades", json=payload)

    assert response.status_code == status
    assert store.host_pool.requests == []
    assert audited == []


async def test_an_unreachable_registry_shows_on_the_page_and_blocks_updating(api):
    client, store, _audited, registry = api
    registry["client"] = _registry(reachable=False)

    listed = _by_runtime((await client.get("/admin/agent/runtime-upgrades")).json())
    assert listed["codex"]["latest_version"] is None
    assert listed["codex"]["latest_error"] == "could not reach the npm registry: ConnectError"
    assert listed["codex"]["update_available"] is False

    response = await client.post(
        "/admin/agent/runtime-upgrades", json={"runtime": "codex", "version": "0.162.1"}
    )
    assert response.status_code == 503
    assert store.host_pool.requests == []


async def test_a_finished_update_shows_as_updated_with_its_rollback(api):
    client, store, _audited, _registry = api
    store.host_pool.selection = {"claude-code": "2.1.300"}
    store.host_pool.upgrades["claude-code"] = {
        "version": "2.1.300",
        "before": "2026-10-15T00:00:00Z",
        "nonce": "0123456789abcdef0123456789abcdef",
        "previous_version": None,
        "requested_at": "2026-10-15T00:00:00+00:00",
        "activated_at": "2026-10-15T00:04:00+00:00",
    }

    item = _by_runtime((await client.get("/admin/agent/runtime-upgrades")).json())["claude-code"]

    assert item["current_version"] == "2.1.300"
    assert item["update_available"] is False
    assert item["upgrade"]["state"] == "updated"
    assert item["upgrade"]["progress"] == 100
    assert item["upgrade"]["previous_version"] is None


async def test_holding_what_roll_back_returns_to_is_not_the_updates_progress(api):
    """Hosts keep that version after the switch; the page follows only the update."""
    client, store, _audited, _registry = api
    held = {"version": "2.1.235", "before": "2026-01-01T00:00:00Z", "nonce": "a" * 32}
    store.host_pool.selection = {"claude-code": "2.1.300"}
    store.host_pool.upgrades["claude-code"] = {
        "kind": "update",
        "version": "2.1.300",
        "before": "2026-10-15T00:00:00Z",
        "nonce": "0123456789abcdef0123456789abcdef",
        "previous_version": "2.1.235",
        "previous_download": held,
        "requested_at": "2026-10-15T00:00:00+00:00",
        "activated_at": "2026-10-15T00:04:00+00:00",
    }
    host = store.host_pool.hosts[0]
    host["runtime_versions_available"] = {"claude-code": ["2.1.220", "2.1.300"]}
    # This host lost its copy and could not download it again.
    host["runtime_downloads"] = [
        {**held, "runtime": "claude-code", "state": "failed", "progress": 0, "error": "npm"}
    ]

    item = _by_runtime((await client.get("/admin/agent/runtime-upgrades")).json())["claude-code"]

    upgrade = item["upgrade"]
    assert (upgrade["state"], upgrade["progress"], upgrade["error"]) == ("updated", 100, None)
    assert upgrade["previous_version"] == "2.1.235"


async def test_the_version_menu_lists_every_stable_release_from_npm(api):
    client, _store, _audited, _registry = api

    response = await client.get("/admin/agent/runtime-upgrades/claude-code/versions")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "runtime": "claude-code",
        "versions": ["2.1.300", "2.1.250", "2.1.220"],
        "deprecated": None,
        "error": None,
    }
    # pi's menu is the package it moved to, which deprecates nothing.
    pi = (await client.get("/admin/agent/runtime-upgrades/pi/versions")).json()
    assert (pi["versions"], pi["deprecated"]) == (["1.2.0", "1.1.0", "0.74.0"], None)


async def test_only_an_agent_the_page_can_update_has_a_version_menu(api):
    client, _store, _audited, _registry = api
    response = await client.get("/admin/agent/runtime-upgrades/kilo/versions")
    assert response.status_code == 422


async def test_an_unreachable_registry_is_the_version_menus_message(api):
    client, _store, _audited, registry = api
    registry["client"] = _registry(reachable=False)

    response = await client.get("/admin/agent/runtime-upgrades/codex/versions")

    assert response.status_code == 200
    assert response.json() == {
        "runtime": "codex",
        "versions": [],
        "deprecated": None,
        "error": "could not reach the npm registry: ConnectError",
    }


async def test_an_older_release_can_be_chosen_too(api):
    """Moving back to a known-good release is the same download and check."""
    client, store, audited, _registry = api
    PUBLISHED.add(("@anthropic-ai/claude-code", "2.1.250"))
    try:
        response = await client.post(
            "/admin/agent/runtime-upgrades", json={"runtime": "claude-code", "version": "2.1.250"}
        )
    finally:
        PUBLISHED.discard(("@anthropic-ai/claude-code", "2.1.250"))

    assert response.status_code == 200
    assert store.host_pool.requests[-1]["version"] == "2.1.250"
    assert audited[-1] == (
        "agent.runtime_version.upgrade",
        {"runtime": "claude-code", "version": "2.1.250", "previous_version": None},
    )
