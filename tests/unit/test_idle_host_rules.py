"""The sweep keeps the host rules renewed on an empty host, and lists what runs behind them."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_chat_group_network import _NAMESPACE, _config, _spec

from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import CHAT_NAMESPACE_LABEL, CHAT_PROBE_LABEL
from cloud_agent_host.environment_tests import environment_run_spec
from cloud_agent_host.idle_host_rules import IdleHostRules, running_deployment_container_ids
from cloud_agent_host.network_guard import GUARD_RENEW_S
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared.errors import SandboxError


class _Docker:
    """Running containers and their labels, answering ``docker container ls``."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, str]] = {}
        self.calls: list[list[str]] = []
        self.fail: Exception | None = None
        self.returncode = 0

    def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.fail is not None:
            raise self.fail
        assert argv[1:3] == ["container", "ls"], argv
        wanted = [argv[i + 1].removeprefix("label=") for i, v in enumerate(argv) if v == "--filter"]
        found = [
            container_id
            for container_id, labels in self.containers.items()
            if all(labels.get(key) == value for key, _, value in (w.partition("=") for w in wanted))
        ]
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            "\n".join(
                f"{container_id}\t{self.containers[container_id].get(CHAT_PROBE_LABEL, '')}"
                for container_id in found
            ),
            "",
        )


class _Guard:
    def __init__(self) -> None:
        self.ensured: list[dict[str, Any]] = []

    def ensure(self, **kwargs: Any) -> None:
        self.ensured.append(kwargs)


def _host(monkeypatch, *, network: bool = True) -> tuple[ChatSandboxManager, _Docker, _Guard]:
    backend = ContainerBackend(
        image="img",
        sandbox_network=_config() if network else None,
        sandbox_namespace=_NAMESPACE,
    )
    guard = _Guard()
    backend._network_guard = guard  # type: ignore[assignment]
    manager = ChatSandboxManager(backend)
    docker = _Docker()
    monkeypatch.setattr(manager, "_run", docker.run)
    return manager, docker, guard


def _labels(spec, namespace: str = _NAMESPACE) -> dict[str, str]:
    return {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: namespace}


def test_an_empty_host_renews_the_rules_without_asking_docker(monkeypatch):
    """Nothing runs behind the rules, but the next start would otherwise renew them itself."""
    manager, docker, guard = _host(monkeypatch)

    manager.network.renew_host_rules()

    assert guard.ensured == [{"max_age_s": GUARD_RENEW_S}]
    assert docker.calls == []


def test_the_listing_names_every_running_container_of_this_deployment(monkeypatch):
    """An environment test or prebuild, and a group no member counts, run on guarded bridges."""
    manager, docker, _guard = _host(monkeypatch)
    docker.containers["env-run"] = _labels(environment_run_spec("envrun_1", 1, None))
    docker.containers["stopping-group"] = _labels(_spec())
    docker.containers["foreign"] = _labels(_spec(), namespace="another-stack")
    docker.containers["probe"] = {**_labels(_spec()), CHAT_PROBE_LABEL: "1"}

    assert running_deployment_container_ids(manager) == ["env-run", "stopping-group"]  # type: ignore[arg-type]
    assert docker.calls == [
        [
            "docker",
            "container",
            "ls",
            "--no-trunc",
            "--format",
            '{{.ID}}\t{{.Label "org.freeinference.chat-probe-created"}}',
            "--filter",
            "label=org.freeinference.chat-sandbox=true",
            "--filter",
            f"label=org.freeinference.sandbox-namespace={_NAMESPACE}",
        ]
    ], "running containers only: a stopped one has nothing behind the rules"


@pytest.mark.parametrize(
    "failure",
    [
        {"fail": SandboxError("Docker lifecycle command failed: timed out")},
        {"returncode": 1},
    ],
)
def test_a_listing_that_fails_answers_nothing(monkeypatch, failure):
    manager, docker, _guard = _host(monkeypatch)
    for name, value in failure.items():
        setattr(docker, name, value)

    assert running_deployment_container_ids(manager) is None  # type: ignore[arg-type]


def test_a_host_without_a_sandbox_network_asks_docker_nothing(monkeypatch):
    manager, docker, guard = _host(monkeypatch, network=False)
    docker.containers["env-run"] = _labels(environment_run_spec("envrun_1", 1, None))

    manager.network.renew_host_rules()

    assert docker.calls == []
    assert guard.ensured == []


def test_a_renewal_that_fails_is_logged_once_per_streak_and_stops_nothing(caplog):
    failures = [2]

    def renew_host_rules() -> None:
        if failures[0]:
            failures[0] -= 1
            raise SandboxError("nft failed: Error: Could not process rule")

    idle = IdleHostRules(
        SimpleNamespace(network=SimpleNamespace(renew_host_rules=renew_host_rules))
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            idle.renew()
        failures[0] = 1
        idle.renew()

    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "shared_chat_idle_host_rules_unavailable"
    ]
    assert len(warnings) == 2, "once per failing streak, not once per pass"
    assert {record.name for record in warnings} == {"cloud_agent_host.shared_session_supervisor"}
