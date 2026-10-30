"""Legacy host environment values move into admin settings once, then obey resets."""

from __future__ import annotations

import json

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.host_pool import hash_secret
from cloud_agent_host.runtime_settings import RuntimeSettings
from cloud_agent_shared.host_settings import RUNNER_SETTING_KEYS, default_values, merge_admin_values

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _imported(_store, host: str) -> bool:
    conn = await asyncpg.connect(**_db_config())
    try:
        return await conn.fetchval(
            "SELECT settings_imported FROM agent_runner_hosts WHERE host = $1", host
        )
    finally:
        await conn.close()


async def test_custom_environment_is_imported_once_and_reset_uses_the_default(store):
    hosts = store.host_pool
    credential = hash_secret("legacy")
    await hosts.register_host(host="runner-a", credential_hash=credential)
    legacy = {
        **default_values(RUNNER_SETTING_KEYS),
        "agent_timeout_s": 7200.0,
        "chat_idle_timeout_s": 600,
        "terminal_input_max_kib": True,
        "unknown": 3,
    }

    first = await hosts.record_host_heartbeat(
        host="runner-a", credential_hash=credential, settings_local=legacy
    )

    assert first["settings_desired"] == {"agent_timeout_s": 7200, "chat_idle_timeout_s": 600}
    assert await _imported(store, "runner-a")
    runtime = RuntimeSettings(legacy)
    runtime.apply(merge_admin_values(await hosts.host_settings(), first["settings_desired"]))
    assert runtime.turn_settings()["agent_timeout_s"] == 7200

    await hosts.set_runner_host_settings(
        host="runner-a", changes={"agent_timeout_s": None, "chat_idle_timeout_s": None}
    )
    later = await hosts.record_host_heartbeat(
        host="runner-a", credential_hash=credential, settings_local=legacy
    )
    assert later["settings_desired"] == {}
    runtime.apply(merge_admin_values(await hosts.host_settings(), later["settings_desired"]))
    assert runtime.turn_settings() == default_values(RUNNER_SETTING_KEYS)


async def test_partial_runner_report_waits_for_the_sandbox_managers_legacy_settings(store):
    hosts = store.host_pool
    credential = hash_secret("legacy")
    await hosts.register_host(
        host="runner-a", credential_hash=credential, sandbox_manager_url="http://sandbox_manager"
    )
    runner_local = {**default_values(RUNNER_SETTING_KEYS), "agent_timeout_s": 7200}
    first = await hosts.record_host_heartbeat(host="runner-a", settings_local=runner_local)
    assert first["settings_desired"] == {"agent_timeout_s": 7200}
    assert not await _imported(store, "runner-a")

    full_local = {**default_values(), **runner_local, "terminal_max_sessions": 99}
    second = await hosts.record_host_heartbeat(host="runner-a", settings_local=full_local)
    assert second["settings_desired"] == {"agent_timeout_s": 7200, "terminal_max_sessions": 99}
    assert await _imported(store, "runner-a")

    third = await hosts.record_host_heartbeat(
        host="runner-a", settings_local={**full_local, "terminal_max_sessions": 120}
    )
    assert third["settings_desired"] == second["settings_desired"]


async def test_absent_local_report_does_not_close_the_legacy_import(store):
    hosts = store.host_pool
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("legacy"))
    await hosts.record_host_heartbeat(host="runner-a")
    assert not await _imported(store, "runner-a")

    report = {**default_values(RUNNER_SETTING_KEYS), "source_import_max_mib": 4096}
    imported = await hosts.record_host_heartbeat(host="runner-a", settings_local=report)
    assert imported["settings_desired"] == {"source_import_max_mib": 4096}
    assert await _imported(store, "runner-a")


@pytest.mark.parametrize("global_change", [False, True])
async def test_admin_reset_before_first_report_prevents_environment_resurrection(
    store, global_change
):
    hosts = store.host_pool
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("legacy"))
    if global_change:
        await hosts.set_host_settings(changes={"agent_timeout_s": None})
    else:
        await hosts.set_runner_host_settings(host="runner-a", changes={"agent_timeout_s": None})

    legacy = {**default_values(RUNNER_SETTING_KEYS), "agent_timeout_s": 7200}
    row = await hosts.record_host_heartbeat(host="runner-a", settings_local=legacy)
    assert row["settings_desired"] == {}
    assert await _imported(store, "runner-a")
    assert (
        merge_admin_values(await hosts.host_settings(), row["settings_desired"])["agent_timeout_s"]
        == 3600
    )


async def test_delayed_report_with_an_old_credential_cannot_import_environment_settings(store):
    hosts = store.host_pool
    old = hash_secret("old")
    await hosts.register_host(host="runner-a", credential_hash=old)
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("new"))

    row = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=old,
        settings_local={**default_values(RUNNER_SETTING_KEYS), "agent_timeout_s": 7200},
    )

    # The whole beat is refused for the replaced credential, import included.
    assert row is None
    assert (await hosts.get_host(host="runner-a"))["settings_desired"] == {}
    assert not await _imported(store, "runner-a")


@pytest.mark.parametrize("global_change", [False, True])
async def test_admin_reset_preserves_unrelated_values_from_a_late_sandbox_manager_report(
    store, global_change
):
    hosts = store.host_pool
    await hosts.register_host(
        host="runner-a",
        credential_hash=hash_secret("legacy"),
        sandbox_manager_url="http://sandbox_manager",
    )
    runner_local = default_values(RUNNER_SETTING_KEYS)
    await hosts.record_host_heartbeat(host="runner-a", settings_local=runner_local)
    assert not await _imported(store, "runner-a")

    changes = {"agent_timeout_s": None, "terminal_max_sessions": None}
    if global_change:
        await hosts.set_host_settings(changes=changes)
    else:
        await hosts.set_runner_host_settings(host="runner-a", changes=changes)
    assert not await _imported(store, "runner-a")

    full_local = {
        **default_values(),
        "agent_timeout_s": 7200,
        "terminal_max_sessions": 99,
        "terminal_idle_ttl_s": 900,
    }
    row = await hosts.record_host_heartbeat(host="runner-a", settings_local=full_local)
    assert row["settings_desired"] == {"terminal_idle_ttl_s": 900}
    assert await _imported(store, "runner-a")
    effective = merge_admin_values(await hosts.host_settings(), row["settings_desired"])
    assert effective["agent_timeout_s"] == 3600
    assert effective["terminal_max_sessions"] == 64
    assert effective["terminal_idle_ttl_s"] == 900


async def test_global_reset_protects_future_hosts_without_discarding_other_legacy_values(store):
    hosts = store.host_pool
    await hosts.set_host_settings(changes={"agent_timeout_s": None})
    await hosts.set_host_settings(changes={"agent_timeout_s": None})
    await hosts.register_host(host="new-runner", credential_hash=hash_secret("legacy"))
    row = await hosts.record_host_heartbeat(
        host="new-runner",
        settings_local={
            **default_values(RUNNER_SETTING_KEYS),
            "agent_timeout_s": 7200,
            "source_import_max_mib": 4096,
        },
    )
    assert row["settings_desired"] == {"source_import_max_mib": 4096}
    assert await _imported(store, "new-runner")
    conn = await asyncpg.connect(**_db_config())
    try:
        assert await conn.fetchval(
            "SELECT host_settings_import_excluded FROM agent_runner_policy WHERE id"
        ) == ["agent_timeout_s"]
    finally:
        await conn.close()


async def test_migration_backfills_valid_custom_values_and_keeps_admin_overrides(store):
    hosts = store.host_pool
    await hosts.register_host(host="runner-a", credential_hash=hash_secret("a"))
    await hosts.register_host(host="runner-b", credential_hash=hash_secret("b"))
    config = _db_config()
    downgrade_to(config, "0035_admin_settings")
    conn = await asyncpg.connect(**config)
    legacy = {
        **default_values(),
        "agent_timeout_s": 7200.0,
        "source_import_max_mib": 6000,
        "chat_idle_timeout_s": 600,
        "shared_resident_capacity": True,
        "terminal_input_max_kib": 128,
        "terminal_max_sessions": "99",
        "workspace_command_output_max_mib": 1.5,
        "unknown": 99,
    }
    try:
        await conn.execute(
            "UPDATE agent_runner_policy SET host_settings = '{\"agent_timeout_s\":900}'::jsonb"
        )
        await conn.execute(
            "UPDATE agent_runner_hosts SET settings_local = $1::jsonb, "
            "settings_desired = '{\"source_import_max_mib\":4096}'::jsonb WHERE host = 'runner-a'",
            json.dumps(legacy),
        )
    finally:
        await conn.close()

    apply_migrations(config)
    row = await hosts.get_host(host="runner-a")
    assert row["settings_desired"] == {"source_import_max_mib": 4096, "chat_idle_timeout_s": 600}
    assert await _imported(store, "runner-a")
    assert not await _imported(store, "runner-b")

    downgrade_to(config, "0035_admin_settings")
    apply_migrations(config)
    assert (await hosts.get_host(host="runner-a"))["settings_desired"] == row["settings_desired"]


async def test_a_late_setting_imports_once_for_a_host_whose_first_import_is_done(store):
    """The runner lease arrived after hosts imported their environment (0107)."""
    hosts = store.host_pool
    credential = hash_secret("legacy")
    await hosts.register_host(host="runner-a", credential_hash=credential)
    # A runner from before the lease setting reports everything but the lease.
    old_report = {
        key: value
        for key, value in {**default_values(RUNNER_SETTING_KEYS), "agent_timeout_s": 7200}.items()
        if key != "lease_ttl_s"
    }
    first = await hosts.record_host_heartbeat(
        host="runner-a", credential_hash=credential, settings_local=old_report
    )
    assert await _imported(store, "runner-a")
    assert first["settings_desired"] == {"agent_timeout_s": 7200}

    # The upgraded runner reports AGENT_LEASE_TTL=300 for the first time.
    new_report = {**old_report, "lease_ttl_s": 300}
    second = await hosts.record_host_heartbeat(
        host="runner-a", credential_hash=credential, settings_local=new_report
    )
    assert second["settings_desired"] == {"agent_timeout_s": 7200, "lease_ttl_s": 300}

    # An admin resets it; the same report must not bring it back.
    await hosts.set_runner_host_settings(host="runner-a", changes={"lease_ttl_s": None})
    third = await hosts.record_host_heartbeat(
        host="runner-a", credential_hash=credential, settings_local=new_report
    )
    assert third["settings_desired"] == {"agent_timeout_s": 7200}
    assert (
        merge_admin_values(await hosts.host_settings(), third["settings_desired"])["lease_ttl_s"]
        == 120
    )


async def test_a_host_reports_the_registry_it_downloads_from(store):
    hosts = store.host_pool
    credential = hash_secret("legacy")
    await hosts.register_host(host="runner-a", credential_hash=credential)
    first = await hosts.record_host_heartbeat(host="runner-a", credential_hash=credential)
    assert first["config_reports"] == {}
    second = await hosts.record_host_heartbeat(
        host="runner-a",
        credential_hash=credential,
        config_reports={"runtime_registry_url": "https://npm.example.org"},
    )
    assert second["config_reports"] == {"runtime_registry_url": "https://npm.example.org"}
    # A beat without the report leaves it in place.
    third = await hosts.record_host_heartbeat(host="runner-a", credential_hash=credential)
    assert third["config_reports"] == {"runtime_registry_url": "https://npm.example.org"}
    listed = {row["host"]: row for row in await hosts.list_runner_hosts()}
    assert listed["runner-a"]["config_reports"]["runtime_registry_url"] == (
        "https://npm.example.org"
    )
