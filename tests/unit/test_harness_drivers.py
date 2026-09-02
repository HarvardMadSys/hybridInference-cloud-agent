"""The live Driver → Session → Turn contract."""

from __future__ import annotations

import pytest

from cloud_agent_host.drivers import TurnResult, get_driver


def test_the_result_contract_is_unrepresentable_to_break():
    with pytest.raises(ValueError, match="non-empty final_output"):
        TurnResult(status="completed", final_output="  ", exit_code=0)


def test_only_registered_live_drivers_resolve():
    for runtime_id in ("claude-code", "codex", "pi", "opencode", "kilo"):
        driver = get_driver(runtime_id)
        capabilities = driver.capabilities()
        assert capabilities.continuity == "resume"
        assert capabilities.resident_transport == "supervisor_live"
        assert capabilities.fork == "at_anchor"
        assert capabilities.concurrent_shared_workspace is True

    with pytest.raises(KeyError):
        get_driver("no-such-harness")


def test_live_drivers_expose_no_one_shot_launch_surface():
    for runtime_id in ("claude-code", "codex", "pi", "opencode", "kilo"):
        driver = get_driver(runtime_id)
        assert not hasattr(driver, "launch")
        assert not hasattr(driver, "launch_resident")
        assert callable(driver.launch_live)
        assert callable(driver.launch_resident_live)
