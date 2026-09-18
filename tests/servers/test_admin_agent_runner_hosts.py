"""Tests for the admin agent runner host pool endpoints.

Ported from the gateway with assertions untouched. One piece of wiring changed
and could not not change: the gateway's ``AppServices`` container became
``app.state.storage``. ``verify_admin_access`` is this repository's own and is
left real — the ``ADMIN_TOKEN`` these tests knock with is the door it
actually opens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_hosts as admin_router
from cloud_agent.api.deps import get_storage
from tests.support.storage import AllCapabilities

ADMIN = {"Authorization": "Bearer test-admin"}


class FakeHostStore(AllCapabilities):
    """The runner-host capability, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        # The singleton policy row: two facts, in one place, as in the store.
        self.active_host: str | None = None
        self.runtime_versions: dict[str, str] = {}
        self.audit: list[dict[str, Any]] = []

    def seed(self, host: str, *, active: bool = False, seen_minutes_ago: float = 0.0) -> None:
        seen = datetime.now(timezone.utc) - timedelta(minutes=seen_minutes_ago)
        self.rows[host] = {
            "host": host,
            "last_worker_id": f"w-{host}",
            "first_seen_at": seen,
            "last_seen_at": seen,
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

    async def record_admin_action(self, **kwargs: Any) -> int:
        self.audit.append(kwargs)
        return len(self.audit)

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
        row["slots_desired"] = slots
        return "ok"


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
    # The refusal has to say how to make it true, or it is a dead end: the
    # version has to be built into the image before it can be picked.
    assert "EXTRA_VERSIONS" in response.json()["detail"]
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
