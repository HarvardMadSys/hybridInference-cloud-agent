"""The registry of runtime settings an admin may set from the Servers page."""

from __future__ import annotations

import pytest

from cloud_agent_shared.host_settings import (
    BROKER_SETTING_KEYS,
    HOST_SETTINGS,
    HOST_SETTINGS_BY_KEY,
    RUNNER_SETTING_KEYS,
    admin_values,
    default_values,
    merge_admin_values,
    reported_values,
    resolve,
    validate_admin_change,
)


def test_every_setting_names_its_host_variable_and_a_default_inside_its_bounds():
    assert len(HOST_SETTINGS_BY_KEY) == len(HOST_SETTINGS)
    assert len({setting.env for setting in HOST_SETTINGS}) == len(HOST_SETTINGS)
    for setting in HOST_SETTINGS:
        assert setting.env.startswith("AGENT_")
        assert setting.minimum <= setting.default <= setting.maximum
        assert setting.accepts(setting.default)


def test_each_setting_is_applied_by_the_runner_the_broker_or_both():
    assert set(HOST_SETTINGS_BY_KEY) == RUNNER_SETTING_KEYS | BROKER_SETTING_KEYS
    assert {"chat_idle_timeout_s"} == RUNNER_SETTING_KEYS & BROKER_SETTING_KEYS
    assert {"terminal_idle_ttl_s", "terminal_hard_ttl_s", "terminal_max_sessions"} <= (
        BROKER_SETTING_KEYS
    )
    assert {"agent_timeout_s", "source_import_max_mib", "shared_resident_capacity"} <= (
        RUNNER_SETTING_KEYS
    )


def test_an_admin_change_accepts_whole_numbers_in_bounds_and_none_to_clear():
    assert validate_admin_change({"agent_timeout_s": 7200, "terminal_max_sessions": None}) == {
        "agent_timeout_s": 7200,
        "terminal_max_sessions": None,
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"no_such_setting": 1}, "unknown setting"),
        ({"agent_timeout_s": 59}, "from 60 through 86400 seconds"),
        ({"shared_resident_capacity": 129}, "from 2 through 128 count"),
        ({"source_import_max_mib": 1.5}, "whole number"),
        ({"source_import_max_mib": True}, "whole number"),
        ({"source_import_max_mib": "4096"}, "whole number"),
        ({"sandbox_cpu_millicores": 99}, "from 100 through 256000 millicores"),
        ({"sandbox_cpu_millicores": 1250.5}, "whole number"),
        ({"sandbox_memory_mib": 127}, "from 128 through 1048576 MiB"),
        ({"sandbox_disk_mib": -1}, "from 0 through 16777216 MiB"),
    ],
)
def test_an_admin_change_is_refused_with_the_setting_and_its_bounds(change, message):
    with pytest.raises(ValueError, match=message):
        validate_admin_change(change)


def test_stored_values_that_can_no_longer_apply_are_dropped_not_clamped():
    stored = {
        "agent_timeout_s": 7200,
        "terminal_max_sessions": 0,
        "retired_setting": 5,
        "source_import_max_mib": "4096",
        "shared_resident_capacity": True,
    }
    assert admin_values(stored) == {"agent_timeout_s": 7200}
    assert admin_values(None) == {}
    assert admin_values(stored, keys={"terminal_max_sessions"}) == {}


def test_disabling_a_server_disk_quota_overrides_a_global_quota():
    assert validate_admin_change({"sandbox_disk_mib": 0}) == {"sandbox_disk_mib": 0}
    values = merge_admin_values({"sandbox_disk_mib": 20480}, {"sandbox_disk_mib": 0})
    assert values["sandbox_disk_mib"] == 0
    assert values["sandbox_cpu_millicores"] == 2000
    assert values["sandbox_memory_mib"] == 4096


def test_a_server_value_beats_the_global_one_and_resolves_over_the_host_default():
    merged = merge_admin_values(
        {"agent_timeout_s": 7200, "terminal_max_sessions": 32},
        {"agent_timeout_s": 900},
    )
    assert merged == {**default_values(), "agent_timeout_s": 900, "terminal_max_sessions": 32}
    local = {"agent_timeout_s": 3600.0, "chat_idle_timeout_s": 15.0}
    # Only the host's own keys come back: the runner never resolves a terminal
    # setting, and the broker never resolves the turn time limit.
    assert resolve(merged, local) == {"agent_timeout_s": 900, "chat_idle_timeout_s": 1800}


def test_reset_emits_every_default_even_if_an_old_host_environment_differs():
    merged = merge_admin_values({}, {})

    assert set(merged) == set(HOST_SETTINGS_BY_KEY)
    assert resolve(merged, {"agent_timeout_s": 9999, "terminal_input_max_kib": 2}) == {
        "agent_timeout_s": 3600,
        "terminal_input_max_kib": 64,
    }


def test_a_report_keeps_known_finite_numbers_and_distinguishes_absent_from_empty():
    assert reported_values(None) is None
    assert reported_values({}) == {}
    assert reported_values(
        {
            "agent_timeout_s": 3600.0,
            "chat_idle_timeout_s": 15.5,
            "terminal_max_sessions": float("inf"),
            "terminal_idle_ttl_s": -1,
            "shared_resident_capacity": True,
            "unknown": 3,
        }
    ) == {"agent_timeout_s": 3600, "chat_idle_timeout_s": 15.5}
