"""Host settings and configuration added for frontend management (WP2).

The runner lease became a heartbeat-delivered host setting; the npm registry
for agent versions became one saved value that every host is told and reports
back; an unusable local sessions-per-sandbox value no longer withholds the
shared-live capabilities.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.test_agent_runner import (  # noqa: F401 - the autouse pytest fixture
    _configure_host,
    _reset_profiling_cache,
    _stamp,
)

from cloud_agent import runtime_upgrades, settings
from cloud_agent.schemas.hosts import HostHeartbeatRequest
from cloud_agent_host import runner as runner_mod
from cloud_agent_host.local_settings import local_shared_resident_capacity
from cloud_agent_host.runtime_downloads import RuntimeDownloads
from cloud_agent_host.runtime_settings import RuntimeSettings
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared.host_settings import (
    HOST_SETTINGS_BY_KEY,
    LATE_IMPORT_KEYS,
    RUNNER_SETTING_KEYS,
    validate_admin_change,
)


@pytest.fixture(autouse=True)
def _no_snapshot():
    settings.reset_settings()
    yield
    settings.reset_settings()


def test_the_lease_is_a_bounded_runner_setting_imported_late() -> None:
    lease = HOST_SETTINGS_BY_KEY["lease_ttl_s"]
    assert lease.env == "AGENT_LEASE_TTL"
    assert lease.scope == "runner"
    assert "lease_ttl_s" in RUNNER_SETTING_KEYS
    assert "lease_ttl_s" in LATE_IMPORT_KEYS
    # Above one heartbeat interval plus one request timeout, at most the cap.
    assert lease.minimum > runner_mod._UNRENEWABLE_LEASE_S
    assert lease.maximum == 900
    with pytest.raises(ValueError, match="Turn lease"):
        validate_admin_change({"lease_ttl_s": 40})
    assert validate_admin_change({"lease_ttl_s": 300}) == {"lease_ttl_s": 300}


def test_the_local_lease_is_reported_and_an_admin_value_replaces_it(monkeypatch) -> None:
    local = runner_mod._runner_local_settings(3600, 300.0)
    assert local["lease_ttl_s"] == 300.0
    runtime = RuntimeSettings(local)
    assert runtime.turn_settings()["lease_ttl_s"] == 300.0
    runtime.apply({**local, "lease_ttl_s": 600})
    assert runtime.turn_settings()["lease_ttl_s"] == 600
    applied, reported_local = runtime.report()
    assert applied["lease_ttl_s"] == 600
    assert reported_local["lease_ttl_s"] == 300.0


def test_a_handed_over_turn_keeps_the_lease_it_was_claimed_with() -> None:
    """The child renews with the lease the parent's claim asked for."""
    settings_seen = runner_mod._turn_host_settings({"lease_ttl_s": 480, "agent_timeout_s": 900})
    assert settings_seen["lease_ttl_s"] == 480.0


@pytest.mark.parametrize("raw", ["1", "500", "eight", "-4"])
def test_an_unusable_local_capacity_is_the_default(monkeypatch, raw) -> None:
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", raw)
    assert local_shared_resident_capacity() == 8


def test_a_usable_local_capacity_is_kept(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "16")
    assert local_shared_resident_capacity() == 16


def test_an_out_of_range_local_capacity_no_longer_withholds_shared_live(monkeypatch) -> None:
    """The P1 debt: a bad bootstrap value hid the host from shared-live placement."""
    _configure_host(monkeypatch)
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "500")
    capabilities = runner_mod._shared_live_host_capabilities(
        ContainerBackend(image="img"), _stamp()
    )
    assert "shared-sandbox-v1" in capabilities


def _downloads(registry: str | None = None) -> RuntimeDownloads:
    return RuntimeDownloads(
        docker="docker",
        image="img",
        namespace="ns",
        image_menu=dict,
        registry=registry,
    )


def test_the_downloader_adopts_the_answered_registry_and_reports_it() -> None:
    downloads = _downloads("https://env.example.org")
    assert downloads.registry_report() == {"runtime_registry_url": "https://env.example.org"}
    # A control plane that does not send one leaves the environment's value.
    assert downloads.set_registry(None) is False
    assert downloads.registry_report() == {"runtime_registry_url": "https://env.example.org"}
    assert downloads.set_registry("https://npm.example.org/") is True
    assert downloads.registry_report() == {"runtime_registry_url": "https://npm.example.org"}
    assert downloads.set_registry("https://npm.example.org") is False
    # Empty is the public registry: no npm_config_registry override.
    assert downloads.set_registry("") is True
    assert downloads.registry_report() == {"runtime_registry_url": ""}


def test_the_heartbeat_keeps_only_known_bounded_config_reports() -> None:
    body = HostHeartbeatRequest.model_validate(
        {
            "config_reports": {
                "runtime_registry_url": "https://npm.example.org",
                "unknown_report": "x",
                "runtime_registry_url_extra": 3,
            }
        }
    )
    assert body.config_reports == {"runtime_registry_url": "https://npm.example.org"}
    assert HostHeartbeatRequest.model_validate({}).config_reports is None
    too_long = HostHeartbeatRequest.model_validate(
        {"config_reports": {"runtime_registry_url": "https://" + "a" * 3000}}
    )
    assert too_long.config_reports == {}


def _hosts(*reports: dict[str, str] | None, status: str = "online") -> list[dict[str, Any]]:
    return [
        {"host": f"runner-{index}", "status": status, "config_reports": report}
        for index, report in enumerate(reports)
    ]


def test_with_the_public_registry_saved_a_host_without_a_report_is_not_lagging(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_REGISTRY_URL", raising=False)
    assert runtime_upgrades.registry_url() == "https://registry.npmjs.org"
    assert runtime_upgrades.registry_lagging_hosts(_hosts(None, {"runtime_registry_url": ""})) == []
    assert runtime_upgrades.registry_lagging_hosts(
        _hosts({"runtime_registry_url": "https://other.example.org"})
    ) == ["runner-0"]


def test_with_another_registry_saved_only_hosts_reporting_it_are_current(monkeypatch) -> None:
    row = settings.initial_settings()
    row.update(settings.encode_changes({"runtime_registry_url": "https://npm.example.org/"}))
    settings.apply_settings(1, row)
    assert runtime_upgrades.registry_url() == "https://npm.example.org"
    hosts = _hosts(
        {"runtime_registry_url": "https://npm.example.org"},
        None,
        {"runtime_registry_url": ""},
    )
    assert runtime_upgrades.registry_lagging_hosts(hosts) == ["runner-1", "runner-2"]
    # Offline hosts are not waited for.
    assert runtime_upgrades.registry_lagging_hosts(_hosts(None, status="offline")) == []


def test_the_registry_cache_is_kept_per_registry(monkeypatch) -> None:
    client = runtime_upgrades.RegistryClient()
    monkeypatch.delenv("AGENT_RUNTIME_REGISTRY_URL", raising=False)
    public = client._cache_key("pkg")
    row = settings.initial_settings()
    row.update(settings.encode_changes({"runtime_registry_url": "https://npm.example.org"}))
    settings.apply_settings(1, row)
    assert client._cache_key("pkg") != public
