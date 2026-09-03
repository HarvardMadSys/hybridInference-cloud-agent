"""The live Driver → Session → Turn contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud_agent_host.drivers import CLAUDE_TOOL_MARKS, LaunchContext, TurnResult, get_driver
from cloud_agent_host.tool_windows import hook_settings


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


def test_claude_live_transport_installs_passive_hooks_in_retained_state():
    updates: list[dict] = []
    context = LaunchContext(
        workdir="/workspace",
        gateway_base_url="http://gateway/v1",
        credential="resident-grant",
        provides_isolation=True,
        state_dir="/agent-state",
        spawn_live=lambda _argv, _env: SimpleNamespace(),
        install_tool_hooks=updates.append,
    )

    session = get_driver("claude-code").launch_live(context)

    assert updates == [hook_settings(CLAUDE_TOOL_MARKS)]
    session.close()
