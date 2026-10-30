"""A heartbeat names the saved agent registry and keeps each host's report (WP2)."""

from __future__ import annotations

from tests.unit.test_host_pool_api import (
    _enroll,
    client as client,
)


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
