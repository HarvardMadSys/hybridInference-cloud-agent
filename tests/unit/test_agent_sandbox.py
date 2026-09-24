"""Tests for the retained-only sandbox substrate and its test seam."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from cloud_agent_host import sandbox as sandbox_mod
from cloud_agent_host.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    ProcessBackend,
    SandboxError,
    SandboxSpec,
    build_backend_from_env,
)
from cloud_agent_shared.egress import SandboxNetworkError, network_from_env

_SPEC = SandboxSpec(argv=["claude", "-p", "hi"], workdir="/tmp/wd", env={"A": "1"})

#: The one network configuration a container backend is built with.
_NET_ENV = {
    "AGENT_SANDBOX_NAMESPACE": "default",
    "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
    "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
    "AGENT_RELAY_NETWORK": "agent-relays",
    "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
    "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
}
_NETWORK = network_from_env(_NET_ENV)


def test_process_backend_refuses_to_run_unisolated_by_default():
    """Running with no isolation must be an explicit operator decision."""
    with pytest.raises(SandboxError) as excinfo:
        ProcessBackend().preflight()
    assert "no isolation" in str(excinfo.value)


def test_process_backend_runs_when_acknowledged():
    """An operator who accepts the risk (ephemeral VM) may proceed."""
    ProcessBackend(acknowledged_unsafe=True).preflight()


def test_backends_report_the_boundary_they_actually_supply():
    """Runtime policy and UI metadata derive from backend capability."""
    process = ProcessBackend(acknowledged_unsafe=True)
    container = ContainerBackend(image="registry.example/agent:1")
    kata = ContainerBackend(image="registry.example/agent:1", runtime=KATA_RUNTIME)

    assert process.provides_isolation is False
    assert process.sandbox_metadata() == {"sandbox_backend": "process"}

    assert container.provides_isolation is True
    assert container.sandbox_metadata() == {
        "sandbox_backend": "container",
        "sandbox_image": "registry.example/agent:1",
    }

    assert kata.provides_isolation is True
    assert kata.sandbox_metadata() == {
        "sandbox_backend": "container",
        "sandbox_image": "registry.example/agent:1",
        "sandbox_runtime": KATA_RUNTIME,
    }


def test_process_backend_executes_and_streams():
    """The process backend really runs a command and yields its output."""
    backend = ProcessBackend(acknowledged_unsafe=True)
    proc = backend.spawn(
        SandboxSpec(argv=["/bin/sh", "-c", "echo one; echo two"], workdir="/tmp", env={})
    )
    lines = [line.strip() for line in proc.lines()]
    assert proc.wait() == 0
    assert lines == ["one", "two"]


def test_process_backend_refuses_persistent_terminals_even_when_acknowledged():
    """A local PTY cannot reliably kill job-control groups across macOS/Linux."""
    backend = ProcessBackend(acknowledged_unsafe=True)
    with pytest.raises(SandboxError, match="container or kata"):
        backend.spawn_terminal(_SPEC, rows=24, cols=80)


def test_container_backend_rejects_removed_disposable_execution():
    """Raw container backends configure retained groups; they never run a turn."""
    backend = ContainerBackend(image="img:1", sandbox_network=_NETWORK)

    with pytest.raises(SandboxError, match="direct container execution was removed"):
        backend.spawn(_SPEC)
    with pytest.raises(SandboxError, match="RetainedChatBackend"):
        backend.spawn_terminal(_SPEC, rows=24, cols=80)


def test_plain_container_is_not_vm_isolated():
    backend = ContainerBackend(image="img:1")
    assert backend.is_vm_isolated is False
    assert backend.runtime is None


def test_kata_backend_selects_the_vm_runtime():
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "kata",
            "AGENT_SANDBOX_IMAGE": "img:1",
            **_NET_ENV,
        }
    )
    assert isinstance(backend, ContainerBackend)
    assert backend.is_vm_isolated is True
    assert backend.runtime == KATA_RUNTIME


def test_backend_selection_from_env():
    """Only an explicit process value selects the non-claimable test seam."""
    with pytest.raises(ValueError, match="AGENT_SANDBOX_IMAGE"):
        build_backend_from_env({})
    assert isinstance(
        build_backend_from_env(
            {"AGENT_SANDBOX_BACKEND": "process", "AGENT_SANDBOX_ALLOW_UNISOLATED": "1"}
        ),
        ProcessBackend,
    )
    assert isinstance(
        build_backend_from_env(
            {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1", **_NET_ENV}
        ),
        ContainerBackend,
    )
    with pytest.raises(SandboxError):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "nonsense"})


def test_explicit_runtime_override_wins():
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_RUNTIME": "runsc",
            "AGENT_SANDBOX_IMAGE": "img:1",
            **_NET_ENV,
        }
    )
    assert isinstance(backend, ContainerBackend)
    assert backend.is_vm_isolated is True
    assert backend.runtime == "runsc"


def test_a_container_backend_without_an_image_refuses() -> None:
    """No default, because an unqualified name is not inert.

    `docker run hybridinference/agent-sandbox` resolves through Docker Hub, so
    a default here hands the container an untrusted agent runs inside to
    whoever registered that namespace.
    """
    with pytest.raises(ValueError, match="AGENT_SANDBOX_IMAGE"):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "container"})
    with pytest.raises(ValueError, match="AGENT_SANDBOX_IMAGE"):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "kata", "AGENT_SANDBOX_IMAGE": "  "})


def test_the_container_backend_does_not_look_for_agent_binaries_on_the_runner():
    """The CLIs live in the sandbox image, not on the host that starts it.

    Probing the runner's PATH here is what made a correctly configured host
    refuse every job with "runtime binary 'claude' is not installed".
    """
    backend = ContainerBackend(image="img")
    assert backend.has_binary("claude") is True
    assert backend.has_binary("definitely-not-installed-anywhere") is True


def test_the_process_backend_does_check_this_host():
    """With no isolation the agent runs here, so here is where it must exist."""
    backend = ProcessBackend(acknowledged_unsafe=True)
    assert backend.has_binary("sh") is True
    assert backend.has_binary("definitely-not-installed-anywhere") is False


def test_the_container_env_is_the_image_s_not_the_runner_s(monkeypatch):
    """HOME must name a directory inside the image, writable by the sandbox user.

    Forwarding the runner's HOME (``/root`` in the Compose deployment) makes
    every agent CLI fail on startup trying to write its config, with an error
    naming a path that does not exist in the container.
    """
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("PATH", "/app/.venv/bin:/usr/bin")

    env = ContainerBackend(image="img").base_env()

    assert env["HOME"] == "/home/agent"
    assert "/app/.venv/bin" not in env["PATH"]


def test_the_process_env_does_inherit_the_host(monkeypatch):
    """The unisolated backend runs here, so the host's environment is correct."""
    monkeypatch.setenv("HOME", "/home/somebody")
    assert ProcessBackend(acknowledged_unsafe=True).base_env()["HOME"] == "/home/somebody"


def test_container_preflight_proves_guest_kernel_with_the_exact_runtime_and_image(monkeypatch):
    # Observer/isolation failures have their own behavioural tests.
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.ChatSandboxManager.probe_guest_observer", lambda self: None
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox_mod.os, "uname", lambda: SimpleNamespace(release="host-kernel"))

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            return SimpleNamespace(returncode=0, stdout="guest-kernel\n", stderr="")
        if argv[1:3] == ["network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if argv[1] == "version" and "{{.Client.Version}}" in argv[-1]:
            return SimpleNamespace(returncode=0, stdout="28.4.0 1.51\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="27.0\n", stderr="")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        ContainerBackend,
        "network_guard",
        lambda self: SimpleNamespace(ensure=lambda **kwargs: None),
    )
    backend = ContainerBackend(
        image="registry.example/sandbox@sha256:abc",
        runtime=KATA_RUNTIME,
        sandbox_network=_NETWORK,
    )

    backend.preflight()

    probe = next(argv for argv in calls if argv[1] == "run")
    assert probe[probe.index("--runtime") + 1] == KATA_RUNTIME
    assert probe[probe.index("--entrypoint") + 2] == "registry.example/sandbox@sha256:abc"
    stamp = backend.version_stamp()
    assert stamp["host_kernel"] == "host-kernel"
    assert stamp["guest_kernel"] == "guest-kernel"


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (0, "host-kernel\n", "", "reported the host kernel"),
        (0, "\n", "", "no guest kernel"),
        (125, "", "runtime failed", "could not start sandbox image"),
    ],
)
def test_container_preflight_refuses_an_unproved_kernel_boundary(
    monkeypatch, returncode, stdout, stderr, message
):
    monkeypatch.setattr(sandbox_mod.os, "uname", lambda: SimpleNamespace(release="host-kernel"))
    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr),
    )
    backend = ContainerBackend(image="img:1", runtime=KATA_RUNTIME)

    with pytest.raises(SandboxError, match=message):
        backend._probe_kernel_boundary()


def test_plain_runc_cannot_fabricate_a_guest_kernel_attestation(monkeypatch):
    monkeypatch.setattr(sandbox_mod.os, "uname", lambda: SimpleNamespace(release="host-kernel"))
    backend = ContainerBackend(image="img:1")

    backend._probe_kernel_boundary()

    stamp = backend.version_stamp()
    assert stamp["sandbox_runtime"] == ""
    assert stamp["host_kernel"] == "host-kernel"
    assert stamp["guest_kernel"] is None


def test_explicit_runc_remains_available_to_the_trusted_profile(monkeypatch):
    """Naming Docker's default runtime must not turn trusted-v1 into a VM."""
    monkeypatch.setattr(sandbox_mod.os, "uname", lambda: SimpleNamespace(release="host-kernel"))
    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runc was probed as a VM")),
    )
    backend = ContainerBackend(image="img:1", runtime="runc")

    backend._probe_kernel_boundary()

    assert backend._kernel_probe == {"host_kernel": "host-kernel", "guest_kernel": None}


def test_custom_runtime_is_reported_exactly_not_relabeled_as_kata(monkeypatch):
    monkeypatch.setattr(sandbox_mod.os, "uname", lambda: SimpleNamespace(release="host-kernel"))
    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="custom-guest\n", stderr=""),
    )
    backend = ContainerBackend(image="img:1", runtime="runsc")

    backend._probe_kernel_boundary()

    stamp = backend.version_stamp()
    assert stamp["sandbox_runtime"] == "runsc"
    assert stamp["guest_kernel"] == "custom-guest"


def test_a_shared_kernel_custom_runtime_gets_no_vm_evidence(monkeypatch):
    monkeypatch.setattr(sandbox_mod.os, "uname", lambda: SimpleNamespace(release="host-kernel"))
    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="host-kernel\n", stderr=""),
    )
    backend = ContainerBackend(image="img:1", runtime="custom-runc")

    backend._probe_kernel_boundary()

    assert backend._kernel_probe["guest_kernel"] is None


def test_a_missing_network_setting_is_refused_not_defaulted_to_the_internet():
    """A missing setting must close the boundary, not open it.

    There is no fallback network: a container backend without its namespace,
    pool, resolver or relay network refuses to be built at all.
    """
    with pytest.raises(SandboxNetworkError):
        build_backend_from_env(
            {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"}
        )
    for missing in (
        "AGENT_SANDBOX_NAMESPACE",
        "AGENT_EGRESS_CHAT_NETWORK_POOL",
        "AGENT_EGRESS_DNS_SERVER",
        "AGENT_RELAY_NETWORK",
    ):
        env = {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1", **_NET_ENV}
        del env[missing]
        with pytest.raises(SandboxNetworkError, match=missing):
            build_backend_from_env(env)


def test_a_backend_built_without_a_network_can_do_no_group_work():
    """Only the candidate check builds one, and it runs no group."""
    backend = build_backend_from_env(
        {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"},
        with_network=False,
    )
    assert isinstance(backend, ContainerBackend)
    with pytest.raises(SandboxError, match="no sandbox network"):
        backend.require_sandbox_network()
    with pytest.raises(SandboxError, match="no sandbox network"):
        backend.network_guard()


def _network_preflight(
    monkeypatch, *, internal: str = "true", exists: bool = True, docker: str = "28.4.0 1.51"
):
    """Run the network half of preflight against a stubbed daemon and guard."""
    calls: list[list[str]] = []
    ensured: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["network", "inspect"]:
            if not exists:
                return SimpleNamespace(returncode=1, stdout="", stderr="No such network")
            return SimpleNamespace(returncode=0, stdout=f"{internal}\n", stderr="")
        if argv[1] == "version" and "{{.Client.Version}}" in argv[-1]:
            return SimpleNamespace(returncode=0, stdout=f"{docker}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="27.0\n", stderr="")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        ContainerBackend,
        "network_guard",
        lambda self: SimpleNamespace(ensure=lambda **kwargs: ensured.append(kwargs)),
    )
    return calls, ensured


def test_preflight_installs_and_reads_back_the_host_rules_before_claiming(monkeypatch):
    calls, ensured = _network_preflight(monkeypatch)
    ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight()

    inspect = next(argv for argv in calls if argv[1:3] == ["network", "inspect"])
    assert inspect[3] == "agent-relays"
    # A fresh verification, not the cached one activations reuse.
    assert ensured == [{"max_age_s": 0}]


def test_a_relay_network_that_routes_out_is_refused(monkeypatch):
    """It is where the relays are found, never a route a sandbox could use."""
    _, ensured = _network_preflight(monkeypatch, internal="false")
    with pytest.raises(SandboxError, match="not `internal`"):
        ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight()
    assert ensured == []


def test_a_missing_relay_network_is_refused(monkeypatch):
    _network_preflight(monkeypatch, exists=False)
    with pytest.raises(SandboxError, match="does not exist"):
        ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight()


@pytest.mark.parametrize("docker", ["27.5.1 1.51", "28.4.0 1.47", "", "garbage"])
def test_a_docker_that_cannot_pin_the_relays_routes_is_refused(monkeypatch, docker):
    """Without gateway priority a group bridge can become a relay's way out."""
    _, ensured = _network_preflight(monkeypatch, docker=docker)
    with pytest.raises(SandboxError, match="Docker 28 or newer"):
        ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight()
    assert ensured == []


def test_the_candidate_preflight_touches_no_network(monkeypatch):
    calls, ensured = _network_preflight(monkeypatch)
    ContainerBackend(image="img:1").preflight(check_network=False)
    assert not [argv for argv in calls if argv[1:3] == ["network", "inspect"]]
    assert ensured == []


def test_every_probe_runs_under_the_runtime_jobs_will_use(monkeypatch):
    """A probe under the daemon's default validates a deployment nobody runs.

    The bind check learned this when it passed on a host with no Kata shim and
    every job then died at spawn. The gateway probe is the same shape — DNS
    and networking are exactly what differs between a shared-kernel container
    and a VM-isolated one — so it carries the flag too, and gets the guest's
    relay addresses the way a chat does.
    """
    backend = build_backend_from_env(
        {"AGENT_SANDBOX_BACKEND": "kata", "AGENT_SANDBOX_IMAGE": "img:1", **_NET_ENV}
    )
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.ChatSandboxManager.relay_addresses",
        lambda self, network: {"agent-gateway": "10.2.0.2"},
    )
    calls = _probe_returns(monkeypatch, "ok")

    backend.check_gateway_reachable("http://agent-gateway:8080")

    runs = [argv for argv in calls if argv[1] == "run"]
    assert len(runs) == 1
    assert runs[0][runs[0].index("--runtime") + 1] == KATA_RUNTIME
    assert "agent-gateway=10.2.0.2" in runs[0]


def _probe_returns(monkeypatch, verdict: str) -> list[list[str]]:
    """Stub the probe container, capturing the argv it would have run."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{verdict}\n", stderr="")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    return calls


def test_a_reachable_gateway_passes(monkeypatch):
    calls = _probe_returns(monkeypatch, "ok")
    backend = ContainerBackend(image="img:1", sandbox_network=_NETWORK)

    backend.check_gateway_reachable("http://agent-gateway:8080")

    argv = calls[0]
    assert argv[:2] == ["docker", "run"]
    assert argv[argv.index("--network") + 1] == "agent-relays"
    # The probe URL is passed as an argument, never interpolated into the
    # script: it comes from configuration and ends up inside a shell.
    assert "http://agent-gateway:8080/health" in argv


def test_a_name_that_does_not_resolve_names_the_remote_gateway_case(monkeypatch):
    _probe_returns(monkeypatch, "unresolved")
    backend = ContainerBackend(image="img:1", sandbox_network=_NETWORK)

    with pytest.raises(SandboxError) as excinfo:
        backend.check_gateway_reachable("http://backend:8080")

    message = str(excinfo.value)
    assert "cannot resolve" in message
    assert "relay" in message


def test_a_relay_that_resolves_but_cannot_forward_is_caught(monkeypatch):
    """The regression this check was rewritten for.

    DNS alone passes for anything with a record — including a tunnel container
    that answers to the gateway's name and cannot reach the gateway behind it.
    That is exactly the shape of a cross-machine deployment, so the one
    topology that needed the check hardest was the one it could not see.
    """
    _probe_returns(monkeypatch, "unreachable")
    backend = ContainerBackend(image="img:1", sandbox_network=_NETWORK)

    with pytest.raises(SandboxError) as excinfo:
        backend.check_gateway_reachable("http://agent-gateway:8080")

    message = str(excinfo.value)
    assert "no HTTP response" in message
    assert "cannot reach the gateway behind it" in message


def test_an_image_without_curl_or_python_degrades_loudly(monkeypatch, caplog):
    """A deployment-owned image may have neither. Resolving is all that can be
    asked of it — but the log must not imply the stronger check ran."""
    _probe_returns(monkeypatch, "dns-only")
    backend = ContainerBackend(image="custom:1", sandbox_network=_NETWORK)

    with caplog.at_level(logging.WARNING):
        backend.check_gateway_reachable("http://agent-gateway:8080")

    assert "agent_sandbox_gateway_probe_degraded" in caplog.text


def test_a_base_url_with_no_host_is_not_probed(monkeypatch):
    calls = _probe_returns(monkeypatch, "ok")
    backend = ContainerBackend(image="img:1", sandbox_network=_NETWORK)

    backend.check_gateway_reachable("not-a-url")

    assert calls == []


def _inspect_json(**overrides):
    import json as _json

    info = {
        "Id": "sha256:localid",
        "RepoDigests": ["registry.example/sandbox@sha256:pinned"],
        "Config": {
            "Labels": {
                "org.freeinference.runtime.claude-code.version": "1.2.3",
                "org.freeinference.runtime.codex.version": "0.5.0",
                "org.freeinference.helper.claude-agent-sdk.version": "0.2.148",
                "org.freeinference.runtime..version": "nameless",
                "org.opencontainers.image.source": "irrelevant",
            }
        },
    }
    info.update(overrides)
    return _json.dumps(info)


def test_version_stamp_reads_runtime_digest_and_baked_versions(monkeypatch):
    """One inspect answers every heartbeat: digest, runtime, CLI versions."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=_inspect_json(), stderr="")

    monkeypatch.setattr("cloud_agent_host.sandbox.subprocess.run", fake_run)
    backend = ContainerBackend(image="img:1", runtime="io.containerd.kata.v2")

    stamp = backend.version_stamp()

    assert stamp["sandbox_runtime"] == "io.containerd.kata.v2"
    assert stamp["sandbox_image_digest"] == "registry.example/sandbox@sha256:pinned"
    # Only well-formed version labels count; a nameless runtime id is dropped.
    assert stamp["runtime_versions"] == {"claude-code": "1.2.3", "codex": "0.5.0"}
    assert stamp["helper_versions"] == {"claude-agent-sdk": "0.2.148"}
    backend.version_stamp()
    assert len(calls) == 1  # cached — the image is pinned for this process


def test_version_stamp_attests_runc_with_an_empty_string(monkeypatch):
    """No VM runtime is a positive statement, not a missing one."""
    monkeypatch.setattr(
        "cloud_agent_host.sandbox.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=_inspect_json(), stderr=""),
    )
    backend = ContainerBackend(image="img:1")

    assert backend.version_stamp()["sandbox_runtime"] == ""


def test_version_stamp_falls_back_to_the_local_image_id(monkeypatch):
    """A built-but-never-pushed image still pins what ran."""
    monkeypatch.setattr(
        "cloud_agent_host.sandbox.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout=_inspect_json(RepoDigests=[]), stderr=""
        ),
    )
    backend = ContainerBackend(image="img:1")

    assert backend.version_stamp()["sandbox_image_digest"] == "sha256:localid"


@pytest.mark.parametrize(
    "failure",
    [OSError("docker unavailable"), subprocess.TimeoutExpired([], 30)],
)
def test_version_stamp_degrades_without_failing_the_heartbeat(monkeypatch, failure):
    """Liveness reporting must not depend on the image store answering."""

    def fail(argv, **kwargs):
        raise failure

    monkeypatch.setattr("cloud_agent_host.sandbox.subprocess.run", fail)
    backend = ContainerBackend(image="img:1", runtime="io.containerd.kata.v2")

    stamp = backend.version_stamp()

    assert stamp["sandbox_runtime"] == "io.containerd.kata.v2"
    assert stamp["sandbox_image_digest"] == ""
    assert stamp["runtime_versions"] == {}


def test_the_process_backend_has_nothing_to_stamp():
    assert ProcessBackend(acknowledged_unsafe=True).version_stamp() == {}
