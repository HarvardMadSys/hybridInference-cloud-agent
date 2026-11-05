"""What a heartbeat stores and answers: its address, its residencies, its configuration."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    _enroll,
    _live_group,
    client,
    residency_client,
)


async def test_a_heartbeat_still_takes_the_address_as_broker_url(client):
    """Hosts of the previous release send that name; the alias is dated."""
    async_client, store = client
    credential = await _enroll(async_client, store)

    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"broker_url": "http://sandbox-manager-a:8092"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.rows["runner-a"]["sandbox_manager_url"] == "http://sandbox-manager-a:8092"


async def test_a_residency_two_groups_report_is_renewed_once_and_lost_once(residency_client):
    """A member that two retained groups both report is still one residency.

    The beat is the host's to shape, so the answer is per residency, not per
    report: a residency that was not extended comes back in ``residencies_lost``
    once.
    """
    client, store = residency_client
    credential = await _enroll(client, store)
    store.beat_answers = {("th-1", 1): "agr-1"}  # th-gone absent → not extended → lost
    group = _live_group(("th-1", 1), ("th-gone", 4))
    second = {**group, "sandbox_id": "asbx_2", "container_id": "fedcba9876543210"}

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={"retained_sandboxes": [group, second]},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["residencies_lost"] == [{"thread_id": "th-gone", "generation": 4}]


async def test_a_heartbeat_only_overwrites_what_it_carries(client):
    """A liveness-only beat must not blank what enrollment declared."""
    async_client, store = client
    credential = await _enroll(async_client, store, slots_total=4, capabilities=["kata"])

    await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert store.rows["runner-a"]["slots_total"] == 4
    assert store.rows["runner-a"]["capabilities"] == ["kata"]


async def test_a_heartbeat_names_the_saved_registry_and_keeps_the_hosts_report(client, monkeypatch):
    """Every host is told the one registry, and says which one it uses (WP2)."""
    from cloud_agent import settings

    async_client, store = client
    credential = await _enroll(async_client, store)
    monkeypatch.setenv("AGENT_RUNTIME_REGISTRY_URL", "https://npm.example.org/")
    settings.reset_settings()
    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"config_reports": {"runtime_registry_url": "", "unknown": "x"}},
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200
    assert response.json()["runtime_registry_url"] == "https://npm.example.org"
    assert store.rows["runner-a"]["config_reports"] == {"runtime_registry_url": ""}
