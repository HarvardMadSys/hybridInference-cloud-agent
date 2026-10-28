"""No Gateway is required or substituted on an explicitly local-account host."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloud_agent_host import runner
from cloud_agent_host.inference import inference_gateway_url
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig, resident_launch
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.sandbox import SandboxError

CONNECTION = {
    "inference_source": "mconn_" + "a" * 32 + "@1",
    "inference_base_url": "https://provider.example/v1",
    "inference_protocol": "chat_completions",
}


def launch(**changes):
    values = {
        "thread_id": "athr_one",
        "runtime_id": "opencode",
        "runtime_version": None,
        "native_session_id": None,
        "model": "owner-model",
        "reasoning_effort": None,
        "gateway_base_url": "",
        "credential": "owner-key",
        **CONNECTION,
        **changes,
    }
    return ResidentLaunch(**values)


def test_local_resident_and_manager_use_the_personal_source_without_a_gateway(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_MODE", "local")
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    assert launch().gateway_base_url == ""
    body = SharedResidentLaunchConfig(
        runtime_id="opencode", model="owner-model", slot_id=1, **CONNECTION
    )
    resident = resident_launch("athr_one", body)
    assert resident.gateway_base_url == ""
    assert resident.inference_base_url == CONNECTION["inference_base_url"]
    assert resident.inference_source == CONNECTION["inference_source"]


@pytest.mark.parametrize("mode", ["gateway", "local"])
def test_a_missing_or_partial_personal_source_never_replaces_a_gateway(monkeypatch, mode):
    monkeypatch.setenv("AGENT_AUTH_MODE", mode)
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    with pytest.raises(ValueError):
        launch(inference_source="")
    with pytest.raises(ValueError):
        launch(inference_base_url="")
    with pytest.raises(ValueError):
        launch(inference_source="", inference_base_url="", inference_protocol="")
    with pytest.raises(SandboxError):
        resident_launch(
            "athr_one", SharedResidentLaunchConfig(runtime_id="opencode", model="m", slot_id=1)
        )


def test_gateway_mode_still_requires_its_relay_even_for_personal_source(monkeypatch):
    monkeypatch.delenv("AGENT_AUTH_MODE", raising=False)
    with pytest.raises(ValueError, match="AGENT_GATEWAY_URL is required"):
        launch()
    assert launch(gateway_base_url="http://agent-gateway:8080").gateway_base_url


def test_local_hosts_refuse_a_gateway_or_a_management_url_as_inference(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_MODE", "local")
    for url in ("http://agent-gateway:8080", "http://agent-control-plane:8000"):
        with pytest.raises(ValueError, match="must not configure"):
            inference_gateway_url(url)
        with pytest.raises(ValueError, match="must not configure"):
            launch(gateway_base_url=url)


@pytest.mark.parametrize(
    "mode,url,expected",
    [
        ("local", "", []),
        ("gateway", "http://agent-gateway:8080", ["http://agent-gateway:8080"]),
    ],
)
def test_runner_probes_only_the_explicit_gateway_before_enrollment(
    monkeypatch, mode, url, expected
):
    monkeypatch.setenv("AGENT_AUTH_MODE", mode)
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    probes = []
    stop = RuntimeError("stopped after inference preflight")
    backend = SimpleNamespace(
        preflight=lambda: None,
        check_gateway_reachable=probes.append,
        version_stamp=Mock(side_effect=stop),
    )
    enrollment = Mock()
    monkeypatch.setattr(runner, "ensure_enrolled", enrollment)
    with pytest.raises(RuntimeError, match="stopped after inference preflight"):
        runner.run_forever(
            base_url="http://agent-control-plane:8000",
            gateway_url=url,
            worker_id="one",
            backend=backend,
        )
    assert probes == expected
    enrollment.assert_not_called()


def test_runner_cli_keeps_management_out_of_the_inference_field(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_MODE", "local")
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    serve = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_forever", serve)
    assert runner.main(["--base-url", "http://agent-control-plane:8000"]) == 0
    assert serve.call_args.kwargs["gateway_url"] == ""
    monkeypatch.setenv("AGENT_AUTH_MODE", "gateway")
    with pytest.raises(SystemExit):
        runner.main(["--base-url", "http://agent-control-plane:8000"])
    assert serve.call_count == 1


def test_invalid_host_mode_and_missing_gateway_fail_before_enrollment(monkeypatch):
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    backend = SimpleNamespace(preflight=lambda: None, check_gateway_reachable=Mock())
    for mode in ("unknown", "gateway"):
        monkeypatch.setenv("AGENT_AUTH_MODE", mode)
        with pytest.raises(ValueError):
            runner.run_forever(base_url="http://control", worker_id="one", backend=backend)
    backend.check_gateway_reachable.assert_not_called()
