"""A broken candidate must be rejected before a serving host is drained."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from tests.unit.test_retained_deploy import _add_required_host_paths, _host, _run

from cloud_agent_host import candidate_check
from cloud_agent_host.candidate_check import check_candidate
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.shared_protocol import (
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
)


def test_candidate_probe_failure_preserves_the_live_marker(tmp_path):
    env = _host(tmp_path, candidate_exit=17)
    _add_required_host_paths(env, tmp_path)
    marker = tmp_path / "app/.shared-live-ready"
    marker.write_text("previous-generation\n")
    env["AGENT_DEPLOYMENT_SHA"] = "target-generation"
    result = _run(env, "up")
    assert result.returncode != 0
    assert marker.read_text() == "previous-generation\n"
    assert "draining existing runners" not in result.stdout
    assert "starting the three" not in result.stdout


def _backend():
    backend = Mock(spec=ContainerBackend)
    backend.version_stamp.return_value = {
        "sandbox_image_digest": "sha256:abc",
        "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
    }
    return backend


@pytest.mark.parametrize(
    "field,key", [("runtime_versions", "codex"), ("helper_versions", "guest-observer")]
)
def test_image_pin_drift_names_the_mismatch(field, key):
    backend = _backend()
    backend.version_stamp.return_value[field][key] = "wrong"
    with pytest.raises(SandboxError, match=key):
        check_candidate(backend)
    backend.preflight.assert_called_once()


def test_real_preflight_failure_is_not_replaced_by_static_success():
    backend = _backend()
    backend.preflight.side_effect = SandboxError("observer boundaries missing")
    with pytest.raises(SandboxError, match="observer boundaries"):
        check_candidate(backend)
    backend.version_stamp.assert_not_called()


def test_the_candidate_installs_no_host_rules():
    """It runs no group, so the live stack's table is not its to touch."""
    backend = _backend()
    check_candidate(backend)
    backend.preflight.assert_called_once_with(check_network=False)


def test_the_candidate_is_built_without_a_deployment_network(monkeypatch):
    """No pool, resolver or relay network exists where a candidate is proved."""
    built = {}

    def build(env, *, with_network):
        built.update(env=env, with_network=with_network)
        return _backend()

    monkeypatch.setattr(candidate_check, "build_backend_from_env", build)
    monkeypatch.setattr(candidate_check, "ContainerBackend", Mock)
    monkeypatch.setenv("AGENT_SANDBOX_NAMESPACE", "freeinference-cloud-agent-sandbox-prod")
    candidate_check.main()
    assert built["with_network"] is False
    # Never the live stack's namespace, so nothing it labels is the stack's.
    assert built["env"]["AGENT_SANDBOX_NAMESPACE"] == "deploy-candidate"


def test_a_candidate_backend_has_no_network_to_guard():
    backend = ContainerBackend(image="candidate")
    with pytest.raises(SandboxError, match="network"):
        backend.require_sandbox_network()
