"""The capabilities a host advertises, its exact deployment once ready, and a claim's."""

from __future__ import annotations

from tests.unit.test_agent_runner import (  # noqa: F401 - the autouse pytest fixture
    _configure_host,
    _reset_profiling_cache,
    _stamp,
)

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.sandbox import ContainerBackend


def test_shared_live_is_the_only_capability_without_feature_switches(monkeypatch):
    _configure_host(monkeypatch)
    # Old rollout variables are deliberately inert when stale values remain in
    # a deployment's .env after the cutover.
    monkeypatch.setenv("AGENT_RESTARTABLE_SANDBOX", "0")
    monkeypatch.setenv("AGENT_SHARED_LIVE_SANDBOX", "0")
    monkeypatch.setenv("AGENT_RESTARTABLE_RUNTIMES", "claude-code")

    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-archive-destination-v1",
        "sandbox-restore-v1",
    ]


def test_exact_deployment_capability_requires_the_completed_marker(monkeypatch, tmp_path):
    _configure_host(monkeypatch)
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "target-sha")
    ready_file = tmp_path / "shared-live-ready"
    monkeypatch.setenv("AGENT_SHARED_LIVE_READY_FILE", str(ready_file))

    ready_file.write_text("pending:target-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-archive-destination-v1",
        "sandbox-restore-v1",
    ]

    ready_file.write_text("different-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-archive-destination-v1",
        "sandbox-restore-v1",
    ]

    ready_file.write_text("target-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-archive-destination-v1",
        "sandbox-restore-v1",
        "shared-sandbox-deployment:target-sha",
    ]


def test_a_claim_attests_the_shared_live_capabilities(monkeypatch):
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)

    assert runner_mod._claim_attestation(backend)["runner_capabilities"] == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-archive-destination-v1",
        "sandbox-restore-v1",
        "environment-v1",
        "environment-inference-v1",
        "environment-snapshot-v1",
        "ssh-access-v1",
        "model-connections-v1",
    ]
