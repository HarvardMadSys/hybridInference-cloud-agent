"""Tests for the retained-only sandbox substrate and its test seam."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from cloud_agent_host import sandbox as sandbox_mod
from cloud_agent_host.egress import EgressPolicyError
from cloud_agent_host.egress_proxy import CANARY_HOST
from cloud_agent_host.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    ProcessBackend,
    SandboxError,
    SandboxSpec,
    build_backend_from_env,
)

_SPEC = SandboxSpec(argv=["claude", "-p", "hi"], workdir="/tmp/wd", env={"A": "1"})


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
    backend = ContainerBackend(image="img:1", network="agent-net")

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
            "AGENT_SANDBOX_NETWORK": "agent-egress",
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
            {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"}
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
            "AGENT_SANDBOX_NETWORK": "agent-egress",
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
        return SimpleNamespace(returncode=0, stdout="27.0\n", stderr="")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    backend = ContainerBackend(
        image="registry.example/sandbox@sha256:abc",
        runtime=KATA_RUNTIME,
        network="agent-egress",
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


def test_an_unset_network_is_refused_not_defaulted_to_the_internet():
    """A missing setting must close the boundary, not open it.

    `network` used to fall back to `bridge`, so forgetting the variable gave
    untrusted repository code unrestricted outbound internet — the opposite of
    the rule the process backend already follows.
    """
    backend = build_backend_from_env(
        {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"}
    )

    assert backend.network == "", "there must be no open-network fallback"
    # And nothing downstream invents one: resolving the agent phase's network
    # is an error rather than a quiet fall back to the Docker bridge.
    with pytest.raises((SandboxError, EgressPolicyError)):
        backend._check_networks_are_closed()
    with pytest.raises(EgressPolicyError):
        backend.network_for_phase("agent")


def test_open_egress_requires_an_explicit_acknowledgement():
    """Same shape as the unisolated-backend opt-in: deliberate, not accidental."""
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_SANDBOX_NETWORK": "some-open-network",
            "AGENT_SANDBOX_ALLOW_OPEN_NETWORK": "1",
        }
    )

    assert backend.allow_open_network is True
    backend._check_networks_are_closed()  # acknowledged: does not raise


def _egress_env(**extra: str) -> dict[str, str]:
    """A container backend with a configured, proxied setup tier."""
    return {
        "AGENT_SANDBOX_BACKEND": "container",
        "AGENT_SANDBOX_IMAGE": "img:1",
        "AGENT_EGRESS_NETWORK_PLATFORM_ONLY": "agent-egress",
        "AGENT_EGRESS_NETWORK_TRUSTED": "agent-egress-trusted",
        "AGENT_EGRESS_PROXY_URL_TRUSTED": "http://agent-egress-proxy:3128",
        **extra,
    }


def _fake_docker(
    monkeypatch,
    *,
    internal: dict[str, str],
    canary: str = "403 403",
    gateway: str = "ok",
):
    """Stand in for the daemon: network internality, and what the canary got.

    ``canary`` is the probe's ``"<cleartext> <connect>"`` line.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=internal.get(argv[3], "true"), stderr="")
        if argv[1] == "run":
            if any(value.endswith("/health") for value in argv if isinstance(value, str)):
                return SimpleNamespace(returncode=0, stdout=gateway, stderr="")
            return SimpleNamespace(returncode=0, stdout=canary, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cloud_agent_host.sandbox.subprocess.run", fake_run)
    return calls


def test_a_proxied_network_that_routes_out_is_refused_too(monkeypatch):
    """The trusted tier is a closed network plus a proxy, not an open network.

    Preflight used to exempt trusted and custom as "allowlist-fronted by
    construction" — an assumption about a network nobody checked. Pointing
    AGENT_EGRESS_NETWORK_TRUSTED at the default bridge then passed in silence
    and handed the setup phase the whole internet, with every log and config
    still reading `trusted`.
    """
    backend = build_backend_from_env(_egress_env())
    _fake_docker(monkeypatch, internal={"agent-egress-trusted": "false"})

    with pytest.raises(SandboxError, match="not `internal`"):
        backend._check_networks_are_closed()


def test_a_proxy_that_serves_the_canary_is_a_startup_failure(monkeypatch):
    """A proxy that answers but allows everything is the worst available outcome.

    The tier reads as allowlisted in every log and config, the network is
    internal exactly as designed, and the sandbox still reaches the internet.
    Nothing downstream would notice — the same shape as a Kata runtime
    degrading to a shared kernel, and checked the same way: by observing the
    behaviour instead of trusting the configuration.
    """
    backend = build_backend_from_env(_egress_env())
    _fake_docker(monkeypatch, internal={}, canary="200 200")

    with pytest.raises(SandboxError, match="reach the internet"):
        backend._check_networks_are_closed()


def test_a_proxy_that_denies_cleartext_but_tunnels_anywhere_is_caught(monkeypatch):
    """The cleartext answer alone proves almost nothing, so it is not trusted alone.

    Our own rendered config denies plain HTTP outright, so a proxy that
    refused cleartext while allowing CONNECT to any host would answer 403 to
    a one-request probe and pass — with every HTTPS destination in the world
    still reachable, which is all a package manager, or an exfiltration
    attempt, actually uses.
    """
    backend = build_backend_from_env(_egress_env())
    _fake_docker(monkeypatch, internal={}, canary="403 200")

    with pytest.raises(SandboxError, match="refuses cleartext and lets the other through"):
        backend._check_networks_are_closed()


def test_an_unreachable_proxy_is_reported_as_unreachable(monkeypatch):
    """Distinguished from "not enforcing", because the fixes are opposite.

    curl reports both as a failure; conflating them would send an operator to
    check the allowlist when the proxy is simply not running. `0` is what
    `%{http_connect}` reports when no CONNECT was ever answered.
    """
    backend = build_backend_from_env(_egress_env())
    _fake_docker(monkeypatch, internal={}, canary="000 0")

    with pytest.raises(SandboxError, match="unreachable"):
        backend._check_networks_are_closed()


def test_a_denying_proxy_passes_and_is_probed_the_way_a_job_would_use_it(monkeypatch):
    """The probe must exercise the mechanism a setup script actually uses.

    Passing `--proxy` on the command line would prove the proxy denies while
    saying nothing about whether the environment reaches the sandbox — which
    is the part that carries the policy.
    """
    backend = build_backend_from_env(_egress_env())
    calls = _fake_docker(monkeypatch, internal={}, canary="403 403")

    backend._check_networks_are_closed()

    probe = next(argv for argv in calls if argv[1] == "run")
    assert probe[probe.index("--network") + 1] == "agent-egress-trusted"
    assert "https_proxy=http://agent-egress-proxy:3128" in probe
    # Both paths, because refusing one and tunnelling the other is a real
    # proxy misconfiguration and only the CONNECT one carries real traffic.
    assert f"http://{CANARY_HOST}/" in probe[-1]
    assert f"https://{CANARY_HOST}/" in probe[-1]
    # And the closed phase is never probed: it has no proxy to enforce anything.
    assert sum(1 for argv in calls if argv[1] == "run") == 1


def test_every_probe_runs_under_the_runtime_jobs_will_use(monkeypatch):
    """A probe under the daemon's default validates a deployment nobody runs.

    The bind check learned this when it passed on a host with no Kata shim and
    every job then died at spawn. The egress probes are the same shape — DNS
    and networking are exactly what differs between a shared-kernel container
    and a VM-isolated one — so they carry the flag too.
    """
    backend = build_backend_from_env(_egress_env(AGENT_SANDBOX_BACKEND="kata"))
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.ChatSandboxManager.fixed_service_addresses",
        lambda self, phase, network: {"agent-gateway": "10.2.0.2"},
    )
    calls = _fake_docker(monkeypatch, internal={})

    backend._check_networks_are_closed()
    backend.check_gateway_reachable("http://backend:8080")

    runs = [argv for argv in calls if argv[1] == "run"]
    assert len(runs) == 2
    for argv in runs:
        assert argv[argv.index("--runtime") + 1] == KATA_RUNTIME


def test_a_custom_tier_keeps_the_operators_own_topology(monkeypatch):
    """`custom` exists to replace our judgement, so neither check applies to it.

    Both exemptions are deliberate and worth pinning, because tightening them
    looks like an improvement. Demanding `internal` would force a transparently
    filtered deployment to set AGENT_SANDBOX_ALLOW_OPEN_NETWORK, which silences
    the check for the *closed* tier too; and probing for our own proxy's 403
    would read an operator's drop-on-deny as "not enforcing" and refuse to
    start a deployment that works.
    """
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_EGRESS_SETUP_TIER": "custom",
            "AGENT_EGRESS_AGENT_TIER": "custom",
            "AGENT_EGRESS_NETWORK_CUSTOM": "operator-net",
            "AGENT_EGRESS_PROXY_URL_CUSTOM": "http://operator-proxy:8080",
        }
    )
    calls = _fake_docker(monkeypatch, internal={"operator-net": "false"}, canary="000")

    backend._check_networks_are_closed()  # does not raise

    assert not [argv for argv in calls if argv[1] == "run"], "a custom proxy must not be probed"


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
    backend = ContainerBackend(image="img:1", network="agent-egress")

    backend.check_gateway_reachable("http://agent-gateway:8080")

    argv = calls[0]
    assert argv[:2] == ["docker", "run"]
    assert "--network" in argv and "agent-egress" in argv
    # The probe URL is passed as an argument, never interpolated into the
    # script: it comes from configuration and ends up inside a shell.
    assert "http://agent-gateway:8080/health" in argv


def test_a_name_that_does_not_resolve_names_the_remote_gateway_case(monkeypatch):
    _probe_returns(monkeypatch, "unresolved")
    backend = ContainerBackend(image="img:1", network="agent-egress")

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
    backend = ContainerBackend(image="img:1", network="agent-egress")

    with pytest.raises(SandboxError) as excinfo:
        backend.check_gateway_reachable("http://agent-gateway:8080")

    message = str(excinfo.value)
    assert "no HTTP response" in message
    assert "cannot reach the gateway behind it" in message


def test_an_image_without_curl_or_python_degrades_loudly(monkeypatch, caplog):
    """A deployment-owned image may have neither. Resolving is all that can be
    asked of it — but the log must not imply the stronger check ran."""
    _probe_returns(monkeypatch, "dns-only")
    backend = ContainerBackend(image="custom:1", network="agent-egress")

    with caplog.at_level(logging.WARNING):
        backend.check_gateway_reachable("http://agent-gateway:8080")

    assert "agent_sandbox_gateway_probe_degraded" in caplog.text


def test_a_base_url_with_no_host_is_not_probed(monkeypatch):
    calls = _probe_returns(monkeypatch, "ok")
    backend = ContainerBackend(image="img:1", network="agent-egress")

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
