"""A broken candidate must be rejected before a serving host is drained."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from tests.unit.test_retained_deploy import _add_required_host_paths, _host, _run

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


def test_docker_null_network_is_accepted_without_allowing_a_bridge(monkeypatch):
    backend = ContainerBackend(image="candidate", network="none")
    backend._check_one_network("none")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: Mock(returncode=0, stdout="false"))
    with pytest.raises(SandboxError, match="not `internal`"):
        backend._check_one_network("bridge")
