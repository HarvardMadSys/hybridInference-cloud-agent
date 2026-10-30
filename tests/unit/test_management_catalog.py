"""The semantic configuration catalog covers the inventory and leaks no value (WP3)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from tests.support import env_inventory as inventory
from tests.unit.test_env_classification import (
    _named,
    scan as scan,
)

from cloud_agent import settings
from cloud_agent.management_catalog import (
    CATALOG,
    CATALOG_BY_ID,
    METHODS,
    NOT_MANAGED,
    NOT_MANAGED_DISPOSITIONS,
    catalog_response,
    item_for_env,
)
from cloud_agent.setting_definition import APPLY_MODES
from cloud_agent_shared.host_settings import HOST_SETTINGS


def _all_inventoried_names(scan) -> set[str]:
    return set(_named(scan)) | {name for family in inventory.FAMILIES for name in family.names}


def test_every_managed_inventory_name_has_an_item(scan) -> None:
    """A new deployment input needs an administrator-facing home or a stated reason."""
    missing = []
    for family in inventory.FAMILIES:
        if family.disposition in NOT_MANAGED_DISPOSITIONS:
            continue
        names = [name for name in _all_inventoried_names(scan) if inventory_family(name) is family]
        for name in names:
            if name in NOT_MANAGED:
                continue
            if item_for_env(name) is None:
                missing.append(f"{name} ({family.disposition})")
    assert missing == []


def inventory_family(name: str):
    for family in inventory.FAMILIES:
        if name in family.names or any(name.startswith(prefix) for prefix in family.prefixes):
            return family
    return None


def test_no_item_names_an_uninventoried_variable(scan) -> None:
    known = _all_inventoried_names(scan)
    stray = [
        (item.id, name)
        for item in CATALOG
        for name in item.env_names
        if not name.endswith("_") and name not in known
    ]
    assert stray == []


def test_every_setting_and_host_setting_belongs_to_exactly_one_item() -> None:
    owners: dict[str, list[str]] = {}
    for item in CATALOG:
        for key in item.setting_keys:
            owners.setdefault(key, []).append(item.id)
    for setting in settings.SETTINGS:
        assert setting.key in owners, setting.key
    for setting in HOST_SETTINGS:
        assert owners.get(setting.key) == ["hosts.runtime_settings"], setting.key


def test_items_use_only_known_vocabulary() -> None:
    assert len(CATALOG_BY_ID) == len(CATALOG)
    for item in CATALOG:
        assert item.method in METHODS
        assert item.apply_mode in (*APPLY_MODES, "per_setting")
        assert item.sources
        if item.method == "apply":
            assert item.role is not None
            assert item.restart_scope


def test_boundary_items_are_marked_bounded() -> None:
    assert CATALOG_BY_ID["hosts.sandbox_boundary"].bounded
    assert CATALOG_BY_ID["hosts.kata"].bounded
    assert item_for_env("AGENT_EGRESS_PROTECTED_CIDRS").id == "hosts.sandbox_boundary"


def test_multi_meaning_items_name_each_source() -> None:
    root = CATALOG_BY_ID["security.settings_root_key"]
    assert {source.meaning for source in root.sources} >= {
        "dedicated root key",
        "AGENT_SESSION_SECRET fallback root",
    }
    assert len(CATALOG_BY_ID["deployment.pgadmin"].sources) == 2


def test_the_admin_token_is_not_described_as_a_narrow_recovery_token() -> None:
    description = CATALOG_BY_ID["security.admin_token"].description
    assert "full administrator" in description


def test_the_catalog_endpoint_is_admin_only_and_carries_no_values(monkeypatch) -> None:
    from cloud_agent.app import create_app

    monkeypatch.setenv("ADMIN_TOKEN", "catalog-admin-token-of-thirty-two-bytes")
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://user:secret-password@db/agent")
    client = TestClient(create_app())
    assert client.get("/admin/agent/management/catalog").status_code in {401, 403}
    response = client.get(
        "/admin/agent/management/catalog",
        headers={"Authorization": "Bearer catalog-admin-token-of-thirty-two-bytes"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == json.loads(json.dumps(catalog_response()))
    text = response.text
    assert "secret-password" not in text
    assert "catalog-admin-token" not in text


@pytest.mark.parametrize("key", ["mcp_allow_private_targets", "default_execution_profile"])
def test_verified_apply_modes_reach_the_settings_definitions(key) -> None:
    definition = settings.SETTINGS_BY_KEY[key].definition()
    assert definition["apply_mode"] != "unverified"
    assert definition["apply_description"]
