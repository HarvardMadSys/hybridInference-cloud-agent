"""Preflight asks Docker once whether it answers and which versions it speaks.

Both checks keep their own refusal: a Docker that does not answer is "not
usable", and one that answers too old for gateway priority is refused for
that. The one answer serves a single preflight only; every other network
check asks again, so nothing the long-lived host reads is ever reused.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import sandbox as sandbox_mod
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_shared.egress import network_from_env

_NETWORK = network_from_env(
    {
        "AGENT_SANDBOX_NAMESPACE": "default",
        "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
        "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
        "AGENT_RELAY_NETWORK": "agent-relays",
        "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
        "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
    }
)

_VERSIONS = ["docker", "version", "--format", "{{.Client.Version}} {{.Server.APIVersion}}"]


def _docker(
    monkeypatch, *, versions: Any = (0, "28.4.0 1.51", "")
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """Answer the Docker CLI; ``versions`` is (status, stdout, stderr) or an exception."""
    calls: list[list[str]] = []
    ensured: list[dict[str, Any]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1] == "version":
            if isinstance(versions, BaseException):
                raise versions
            status, stdout, stderr = versions
            return SimpleNamespace(returncode=status, stdout=f"{stdout}\n", stderr=stderr)
        if argv[1:3] == ["network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="not in this test")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", run)
    monkeypatch.setattr(
        ContainerBackend,
        "network_guard",
        lambda self: SimpleNamespace(ensure=lambda **kwargs: ensured.append(kwargs)),
    )
    return calls, ensured


def test_a_runc_preflight_runs_one_docker_version_for_both_checks(monkeypatch):
    calls, ensured = _docker(monkeypatch)

    ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight()

    assert [argv for argv in calls if argv[1] == "version"] == [_VERSIONS]
    assert calls[0] == _VERSIONS, "Docker is asked before anything else"
    assert ensured == [{"max_age_s": 0}]


@pytest.mark.parametrize("check_network", [True, False])
@pytest.mark.parametrize(
    ("versions", "reason"),
    [
        pytest.param((1, "", "Cannot connect to the Docker daemon"), "Cannot connect", id="down"),
        pytest.param(FileNotFoundError("docker"), "docker", id="missing"),
        pytest.param(subprocess.TimeoutExpired("docker", 20), "timed out", id="hung"),
    ],
)
def test_a_docker_that_does_not_answer_is_still_not_usable(
    monkeypatch, versions, reason, check_network
):
    """Refused as before, before the version is read and before any other Docker process."""
    calls, ensured = _docker(monkeypatch, versions=versions)

    with pytest.raises(SandboxError, match=r"docker is not usable: .*" + reason):
        ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight(
            check_network=check_network
        )

    assert calls == [_VERSIONS]
    assert ensured == []


def test_a_docker_that_answers_too_old_is_refused_for_gateway_priority(monkeypatch):
    """Usable, so the first check passes; the same answer then fails the second."""
    calls, ensured = _docker(monkeypatch, versions=(0, "27.5.1 1.47", ""))

    with pytest.raises(SandboxError, match=r"Docker 28 or newer.*client 27\.5\.1.*API 1\.47"):
        ContainerBackend(image="img:1", sandbox_network=_NETWORK).preflight()

    assert calls == [_VERSIONS]
    assert ensured == []


def test_a_preflight_without_the_network_check_still_proves_docker_answers(monkeypatch):
    """The candidate check: only whether Docker answers, which an old one does."""
    calls, _ensured = _docker(monkeypatch, versions=(0, "27.5.1 1.47", ""))

    ContainerBackend(image="img:1").preflight(check_network=False)

    assert calls == [_VERSIONS]


def test_every_network_check_outside_a_preflight_asks_docker_again(monkeypatch):
    """The host service's checks, and a Kata child's with its host's verdict, read afresh."""
    calls, _ensured = _docker(monkeypatch)
    backend = ContainerBackend(image="img:1", sandbox_network=_NETWORK)

    backend.preflight()
    backend.check_network()
    backend.check_network()

    assert [argv for argv in calls if argv[1] == "version"] == [_VERSIONS] * 3
