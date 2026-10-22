"""Bounded execs enter the container a start or proof verified, without inspecting it first.

A container's labels, mounts, tmpfs, runtime and image never change for its
id, and Docker refuses an exec into one that is stopped or removed. These
tests hold the manager to the rest of that bargain: one Docker process per
exec into a proven container, the error an inspection first would have raised
when Docker refuses one, no entry into a same-name replacement, and the
inspections that stay (archive reads, spawns, the host listing's ownership).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from typing import Any

import pytest

from cloud_agent_host import chat_sandbox as chat_sandbox_mod
from cloud_agent_host.chat_sandbox import (
    CHAT_NAMESPACE_LABEL,
    CHAT_PROBE_LABEL,
    ENVIRONMENT_TEST_LABEL,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatLost,
)
from cloud_agent_host.chat_sandbox_exec import ProvenContainers
from cloud_agent_host.chat_sandbox_inspect import INSPECT_BATCH
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxError

_NAMESPACE = "this-stack"
#: ``docker exec`` options that take a value, before the container is named.
_VALUED = {"--user", "--workdir", "--env"}


class _Spawned(Exception):
    """Raised by the test's Popen once it has seen the argv it would have run."""


def _exec_target(argv: list[str]) -> str:
    """The container a ``docker exec`` argv names."""
    options = iter(argv[2:])
    for option in options:
        if option in _VALUED:
            next(options)
        elif not option.startswith("--"):
            return option
    raise AssertionError(f"no container in {argv}")


def _spec(**changes: Any) -> ChatSandboxSpec:
    spec = ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
        sandbox_image_digest="sha256:image-pinned",
    )
    return replace(spec, **changes)


class _Docker:
    """Containers by id and name, answering ``inspect``, ``ls`` and ``exec`` as the CLI does."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        #: The exit status of a command that runs.
        self.exit_code = 0

    def add(
        self,
        container_id: str,
        spec: ChatSandboxSpec,
        *,
        running: bool = True,
        runtime: str = "",
        labels: dict[str, str] | None = None,
    ) -> None:
        self.containers[container_id] = {
            "Id": container_id,
            "Name": "/" + spec.container_name,
            "Config": {
                "Image": spec.sandbox_image_digest,
                "Labels": {
                    **spec.docker_labels(),
                    CHAT_NAMESPACE_LABEL: _NAMESPACE,
                    **(labels or {}),
                },
            },
            "State": {"Status": "running" if running else "exited", "Running": running},
            "HostConfig": {"Runtime": runtime},
            "Mounts": [],
            "NetworkSettings": {"Networks": {"agent-net": {}}},
        }

    def find(self, ref: str) -> dict[str, Any] | None:
        return next(
            (raw for raw in self.containers.values() if ref in (raw["Id"], raw["Name"][1:])),
            None,
        )

    def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """The manager's ``_run``: lifecycle reads, and the privileged helper's exec."""
        self.calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            found = [self.find(ref) for ref in argv[3:]]
            missing = [
                f"Error: No such container: {ref}"
                for ref, raw in zip(argv[3:], found, strict=True)
                if raw is None
            ]
            payload = json.dumps([raw for raw in found if raw is not None])
            return subprocess.CompletedProcess(
                argv, 1 if missing else 0, payload, "\n".join(missing)
            )
        if argv[1:3] == ["container", "ls"]:
            rows = [
                {
                    "id": raw["Id"],
                    "namespace": raw["Config"]["Labels"].get(CHAT_NAMESPACE_LABEL, ""),
                    "probe": raw["Config"]["Labels"].get(CHAT_PROBE_LABEL, ""),
                }
                for raw in self.containers.values()
            ]
            return subprocess.CompletedProcess(argv, 0, "\n".join(map(json.dumps, rows)), "")
        if argv[1] == "exec":
            code, stderr = self._answer(argv)
            return subprocess.CompletedProcess(argv, code, "", stderr)
        raise AssertionError(f"unmodelled docker command: {argv}")

    def capture(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """``subprocess.run`` for a bounded exec."""
        self.calls.append(list(argv))
        code, stderr = self._answer(argv)
        return subprocess.CompletedProcess(argv, code, b"", stderr.encode())

    def _answer(self, argv: list[str]) -> tuple[int, str]:
        """Run an exec the way Docker does: only in a running container that exists."""
        target = _exec_target(argv)
        raw = self.find(target)
        if raw is None:
            return 1, f"Error response from daemon: No such container: {target}"
        if not raw["State"]["Running"]:
            return 1, f"Error response from daemon: container {target} is not running"
        return self.exit_code, ""

    def targets(self) -> list[str]:
        """The containers each exec so far was sent to, as Docker received them."""
        return [_exec_target(argv) for argv in self.calls if argv[1] == "exec"]

    def inspections(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[1:3] == ["container", "inspect"]]


def _world(monkeypatch, *, runtime: str | None = None):
    backend = ContainerBackend(image="img", runtime=runtime, sandbox_namespace=_NAMESPACE)
    manager = ChatSandboxManager(backend)
    docker = _Docker()
    monkeypatch.setattr(manager, "_run", docker.run)
    monkeypatch.setattr(chat_sandbox_mod.subprocess, "run", docker.capture)
    # The group network is not what these tests are about: its proof is the
    # one test_chat_group_network.py runs against a model of Docker.
    monkeypatch.setattr(
        manager, "_require_group_network", lambda spec, _current, *_snapshot: manager._require(spec)
    )
    monkeypatch.setattr(manager, "_sync_kata_guest_network", lambda *_args: None)
    return manager, docker


def _proven(monkeypatch, *, runtime: str | None = None, container_runtime: str = ""):
    manager, docker = _world(monkeypatch, runtime=runtime)
    spec = _spec()
    docker.add("container-one", spec, runtime=container_runtime)
    manager.start(spec)
    docker.calls.clear()
    return manager, docker, spec


def test_a_started_container_is_entered_by_id_with_no_inspection_first(monkeypatch):
    manager, docker, spec = _proven(monkeypatch)

    result = manager.exec_capture(spec, ["true"])

    assert result.returncode == 0
    assert docker.inspections() == []
    assert docker.targets() == ["container-one"], "one Docker process, sent to the exact id"


def test_a_network_proof_makes_the_same_promise(monkeypatch):
    manager, docker = _world(monkeypatch)
    spec = _spec()
    docker.add("container-one", spec)
    monkeypatch.setattr(manager, "network_for", lambda _spec: "agent-net")
    manager.require_network(spec)
    docker.calls.clear()

    manager.exec_capture(spec, ["true"])

    assert docker.inspections() == []


def test_without_a_proof_an_exec_still_inspects_first(monkeypatch):
    manager, docker = _world(monkeypatch)
    spec = _spec()
    docker.add("container-one", spec)

    manager.exec_capture(spec, ["true"])

    assert [argv[1] for argv in docker.calls] == ["container", "exec"]
    assert docker.targets() == ["container-one"]


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ("stopped", SandboxError, "must be started before exec"),
        ("removed", SandboxError, "chat sandbox is missing"),
        ("replaced", RetainedChatLost, "identity changed"),
    ],
)
def test_a_refused_exec_raises_what_an_inspection_first_would_have(
    monkeypatch, change, error, message
):
    manager, docker, spec = _proven(monkeypatch)
    if change == "stopped":
        docker.containers["container-one"]["State"] = {"Status": "exited", "Running": False}
    else:
        docker.containers.pop("container-one")
    if change == "replaced":
        # The same name and labels, as a recreation would have them.
        docker.add("container-two", spec)

    with pytest.raises(error, match=message):
        manager.exec_capture(spec, ["true"])

    assert docker.targets() == ["container-one"], "a replacement is never entered"
    assert len(docker.inspections()) == 1, "inspected once, after Docker refused"


def test_a_command_that_fails_in_a_running_proven_container_keeps_its_result(monkeypatch):
    manager, docker, spec = _proven(monkeypatch)
    docker.exit_code = 3

    result = manager.exec_capture(spec, ["false"])

    assert result.returncode == 3
    assert len(docker.inspections()) == 1, "the inspection an exec used to begin with, after it"


def test_an_exact_archive_read_still_inspects_its_container_first(monkeypatch):
    manager, docker, spec = _proven(monkeypatch)

    manager.exec_capture(spec, ["true"], expected_container_id="container-one")

    assert docker.calls[0] == ["docker", "container", "inspect", "container-one"]
    assert docker.targets() == ["container-one"]


def test_a_spawn_still_inspects_first_and_runs_in_that_container_by_id(monkeypatch):
    manager, docker, spec = _proven(monkeypatch)
    spawned: list[list[str]] = []

    def popen(argv, **_kwargs):
        spawned.append(argv)
        raise _Spawned

    monkeypatch.setattr(chat_sandbox_mod.subprocess, "Popen", popen)

    with pytest.raises(_Spawned):
        manager.spawn(spec, ["sleep", "1"])

    assert docker.inspections() == [["docker", "container", "inspect", spec.container_name]]
    assert _exec_target(spawned[0]) == "container-one", "never by the reusable name"


def test_a_spawn_refuses_a_same_name_replacement_of_the_proven_container(monkeypatch):
    manager, docker, spec = _proven(monkeypatch)
    docker.containers.pop("container-one")
    docker.add("container-two", spec)
    monkeypatch.setattr(
        chat_sandbox_mod.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("spawned")
    )

    with pytest.raises(RetainedChatLost, match="identity changed"):
        manager.spawn(spec, ["sleep", "1"])
    with pytest.raises(RetainedChatLost, match="identity changed"):
        manager.spawn_terminal(spec, rows=24, cols=80)


def test_a_privileged_helper_enters_a_proven_kata_container_by_id(monkeypatch):
    manager, docker, spec = _proven(
        monkeypatch, runtime=KATA_RUNTIME, container_runtime=KATA_RUNTIME
    )

    manager._kata_privileged_exec(spec, ["true"])

    assert docker.inspections() == []
    assert docker.calls == [
        [
            "docker",
            "exec",
            "--privileged",
            "--user",
            "0:0",
            "--workdir",
            "/",
            "container-one",
            "true",
        ]
    ]


def test_a_privileged_helper_is_still_refused_a_proven_container_not_under_kata(monkeypatch):
    manager, docker, spec = _proven(monkeypatch, runtime=KATA_RUNTIME, container_runtime="runc")

    with pytest.raises(SandboxError, match="inspected Kata container"):
        manager._kata_privileged_exec(spec, ["true"])

    assert docker.calls == []


def test_a_proof_names_one_spec_and_goes_with_its_container():
    proven = ProvenContainers()
    spec = _spec()
    inspection = object()

    assert proven.record(spec, inspection) is inspection  # type: ignore[arg-type]
    assert proven.get(spec) is inspection
    assert proven.get(_spec(generation=4)) is None, "another generation inspects first"
    proven.forget(spec)
    assert proven.get(spec) is None


def test_creating_a_container_forgets_the_proof_its_name_held(monkeypatch):
    manager, docker, spec = _proven(monkeypatch)
    docker.containers.pop("container-one")
    monkeypatch.setattr(manager, "require_host_rules", lambda: None)
    run = docker.run

    def create(argv, **kwargs):
        if argv[1] == "create":
            docker.add("container-two", spec, running=False)
            return subprocess.CompletedProcess(argv, 0, "container-two\n", "")
        return run(argv, **kwargs)

    monkeypatch.setattr(manager, "_run", create)

    manager.create(spec)

    assert manager._proven.get(spec) is None


# ── the host listing ──────────────────────────────────────────────────


def test_the_listing_inspects_every_owned_container_in_one_process(monkeypatch):
    manager, docker = _world(monkeypatch)
    for index in range(3):
        docker.add(f"owned-{index}", _spec(thread_id=f"athr_{index}", sandbox_id=f"asbx_{index}"))
    docker.add(
        "env-test",
        _spec(thread_id="athr_e", sandbox_id="asbx_e"),
        labels={ENVIRONMENT_TEST_LABEL: "1"},
    )
    docker.add(
        "foreign",
        _spec(thread_id="athr_f", sandbox_id="asbx_f"),
        labels={CHAT_NAMESPACE_LABEL: "sibling"},
    )
    docker.add(
        "probe", _spec(thread_id="athr_p", sandbox_id="asbx_p"), labels={CHAT_PROBE_LABEL: "1"}
    )

    listed = manager.list()

    assert [item.container_id for item in listed] == ["owned-0", "owned-1", "owned-2"]
    assert docker.inspections() == [
        ["docker", "container", "inspect", "owned-0", "owned-1", "owned-2", "env-test"]
    ], "ownership comes from ls: a foreign container or a probe is never inspected"


def test_the_listing_skips_a_container_gone_since_ls_but_not_a_daemon_error(monkeypatch):
    manager, docker = _world(monkeypatch)
    docker.add("owned-0", _spec(thread_id="athr_0", sandbox_id="asbx_0"))
    docker.add("owned-1", _spec(thread_id="athr_1", sandbox_id="asbx_1"))
    run = docker.run

    def removed_after_ls(argv, **kwargs):
        if argv[1:3] == ["container", "inspect"]:
            docker.containers.pop("owned-1", None)
        return run(argv, **kwargs)

    monkeypatch.setattr(manager, "_run", removed_after_ls)
    assert [item.container_id for item in manager.list()] == ["owned-0"]

    error = "Cannot connect to the Docker daemon"

    def daemon_error(argv, **kwargs):
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, "", error)
        return run(argv, **kwargs)

    monkeypatch.setattr(manager, "_run", daemon_error)
    with pytest.raises(SandboxError, match="Cannot connect"):
        manager.list()
    # "Not found" only means a container that went away when it names one.
    error = "Error response from daemon: plugin not found"
    with pytest.raises(SandboxError, match="plugin not found"):
        manager.list()


def test_a_large_listing_is_inspected_in_bounded_batches(monkeypatch):
    manager, docker = _world(monkeypatch)
    count = 2 * INSPECT_BATCH + 7
    for index in range(count):
        docker.add(f"owned-{index}", _spec(thread_id=f"athr_{index}", sandbox_id=f"asbx_{index}"))

    assert len(manager.list()) == count
    assert [len(argv) - 3 for argv in docker.inspections()] == [INSPECT_BATCH, INSPECT_BATCH, 7]
