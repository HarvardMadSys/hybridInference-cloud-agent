"""Releasing a node stops its runner and leaves its inventory readable, on real Docker.

Each test gets a node of its own: a Docker-in-Docker container with an empty
daemon. The control plane's own ``quiesce`` and ``inventory`` run there as
they run on a real node, through a transport that executes the same command
line it would send over SSH. Inside the node:

* a ``runner`` Compose service under the deployment's project that, like a
  runner whose credential was refused before #844, creates a self-test
  sandbox on every start and exits, so ``unless-stopped`` restarts it;
* the ``sandbox-manager`` service, which release must leave running.

The self-test's labels are not written out here: they are captured from the
runner's own probe (``_probe_guest_observer_once``), so this test follows
any change to them.

Unit tests stand in for Docker with a fake whose stopped runner stays stopped
and whose listing cannot race a removal. Staging found both false
(2026-10-20) while every unit test passed.

Run by hand with ``pytest -m external tests/external/test_host_release_real_docker.py``.
Needs Docker with ``--privileged`` and the local image ``alpine:3.20``
(``AGENT_HOST_NODE_TEST_IMAGE`` overrides it); the node image is built from
``tests/external/host_node``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import time
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cloud_agent.ports.external.compute_provider import (
    NodeObservation,
    ProviderError,
    SshConnection,
)
from cloud_agent.ports.external.host_bootstrap import BootstrapIntent, DeploymentProfile
from cloud_agent.providers.compute.host_bootstrap import SshHostBootstrap
from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend, SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.external

BASE = os.environ.get("AGENT_HOST_NODE_TEST_IMAGE", "alpine:3.20")
NODE_IMAGE = "cloud-agent-test-node:local"
NODE_CONTEXT = Path(__file__).parent / "host_node"
ROOT = "/opt/cloud-agent-release"
PROFILE = DeploymentProfile(
    id="release",
    label="Release",
    source_bundle="/dev/null",
    source_sha256="0" * 64,
    deployment_sha="a" * 40,
    environment_ref="deployment:env",
    deploy_root=ROOT,
)
INTENT = BootstrapIntent(
    operation_id="operation-1",
    allocation_id="allocation-1",
    node_id="node-1",
    host="managed-host-1",
)
PROJECT = INTENT.project
NODE = NodeObservation(
    provider_node_id="node-1",
    instance_identity="allocation:machine",
    display_name="node",
    machine_identity="verified",
    connection=SshConnection(
        address="node.invalid",
        username="agent",
        credential_ref="ssh",
        known_hosts_ref="ssh:known_hosts",
    ),
)


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kwargs)


def _labels(command: list[str]) -> list[str]:
    """Every ``--label`` of a ``docker create`` command line, as ``--label`` pairs."""
    pairs: list[str] = []
    for flag, value in pairwise(command):
        if flag == "--label":
            pairs += ["--label", value]
    return pairs


def _probe_labels(namespace: str) -> list[str]:
    """The labels the runner's own self-test gives its sandbox."""
    manager = ChatSandboxManager(ContainerBackend(image=BASE, sandbox_namespace=namespace))
    created: list[list[str]] = []

    def refuse(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        created.append(command)
        return subprocess.CompletedProcess(command, 1, "", "captured")

    manager._run = refuse  # type: ignore[method-assign]
    with pytest.raises(SandboxError):
        manager._probe_guest_observer_once()
    (create,) = [command for command in created if command[1:2] == ["create"]]
    return _labels(create)


def _sandbox_labels(namespace: str) -> list[str]:
    """The labels of an owner's chat sandbox, which release must keep."""
    manager = ChatSandboxManager(ContainerBackend(image=BASE, sandbox_namespace=namespace))
    spec = ChatSandboxSpec(
        thread_id="athr_owner", sandbox_id="asbx_owner", generation=1, runtime_id="claude-code"
    )
    return _labels(manager.build_create_command(spec))


class Node:
    """One disposable node: its own Docker daemon, reached with ``docker exec``."""

    def __init__(self, name: str) -> None:
        self.name = name

    def sh(self, script: str, *, check: bool = True) -> str:
        return _docker("exec", self.name, "sh", "-c", script, check=check).stdout

    def docker(self, *args: str, check: bool = True) -> str:
        return _docker("exec", self.name, "docker", *args, check=check).stdout

    def containers(self, *filters: str) -> list[dict[str, str]]:
        listing = self.docker(
            "ps",
            "-a",
            *filters,
            "--format",
            '{"name":{{json .Names}},"state":{{json .State}},'
            '"service":{{json (.Label "com.docker.compose.service")}}}',
        )
        return [json.loads(line) for line in listing.splitlines()]

    def probes(self, namespace: str = PROJECT) -> list[str]:
        return self.docker(
            "ps",
            "-aq",
            "--filter",
            "label=org.freeinference.chat-probe-created",
            "--filter",
            "label=org.freeinference.sandbox-namespace=" + namespace,
        ).split()

    def service(self, name: str) -> dict[str, str]:
        (service,) = [c for c in self.containers() if c["service"] == name]
        return service

    def restarts(self, name: str) -> int:
        container = self.docker(
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={name}",
        ).split()[0]
        return int(self.docker("inspect", "--format", "{{.RestartCount}}", container))


class NodeTransport:
    """Runs the command line ``SshHostBootstrap`` would send over SSH, on the node."""

    def __init__(self, node: Node) -> None:
        self.node = node

    async def run(self, connection, script: str, *, stdin: bytes | None = None, timeout=30) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            self.node.name,
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(process.communicate(stdin or b""), timeout)
        if process.returncode:
            raise ProviderError("ssh_command_failed", "The node command failed")
        return out.decode()


@pytest.fixture(scope="module")
def node_image() -> str:
    if shutil.which("docker") is None or _docker("image", "inspect", BASE, check=False).returncode:
        pytest.skip(f"needs Docker and the local image {BASE}")
    _docker("build", "-q", "-t", NODE_IMAGE, str(NODE_CONTEXT))
    return NODE_IMAGE


@pytest.fixture
def node(node_image: str) -> Iterator[Node]:
    name = f"ca-release-node-{secrets.token_hex(4)}"
    _docker("run", "-d", "--privileged", "--name", name, "-e", "DOCKER_TLS_CERTDIR=", node_image)
    try:
        node = Node(name)
        deadline = time.monotonic() + 60
        while _docker("exec", name, "docker", "info", check=False).returncode:
            if time.monotonic() > deadline:
                pytest.fail("the node's Docker daemon did not start")
            time.sleep(0.5)
        image = subprocess.run(["docker", "save", BASE], capture_output=True, check=True).stdout
        subprocess.run(
            ["docker", "exec", "-i", name, "docker", "load", "-q"],
            input=image,
            capture_output=True,
            check=True,
        )
        # The deployment the release checks for: its identity marker, as a
        # deploy writes it.
        identity = SshHostBootstrap._params(PROFILE, INTENT)["identity"]
        node.sh(
            f"mkdir -p {ROOT} && printf '%s' '{json.dumps(identity)}' > {ROOT}/.compute-identity.json"
        )
        yield node
    finally:
        _docker("rm", "--force", "--volumes", name, check=False)


def _start_stack(node: Node) -> None:
    """The deployment's Compose project: a restarting runner and its sandbox manager."""
    probe = " ".join(_probe_labels(PROJECT))
    compose = {
        "services": {
            "runner": {
                "image": BASE,
                "restart": "unless-stopped",
                "volumes": [
                    "/usr/local/bin/docker:/usr/local/bin/docker:ro",
                    "/var/run/docker.sock:/var/run/docker.sock",
                ],
                # What a refused runner did on every start: qualify the host
                # with a fresh self-test sandbox, then exit.
                "command": ["sh", "-c", f"docker run -d {probe} {BASE} sleep 1d; sleep 1"],
            },
            "sandbox-manager": {"image": BASE, "command": ["sleep", "1d"]},
        }
    }
    node.sh(f"printf '%s' '{json.dumps(compose)}' > {ROOT}/compose.yml")
    node.sh(f"docker compose -p {PROJECT} -f {ROOT}/compose.yml up -d")


def _wait(condition, message: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(message)
        time.sleep(0.5)


@pytest.mark.asyncio
async def test_release_stops_a_restarting_runner_and_its_inventory_comes_back_clean(node):
    """The staging acceptance of 2026-10-20, which release used to refuse forever."""
    _start_stack(node)
    # The restart loop is real: each start left one more self-test behind.
    _wait(lambda: len(node.probes()) >= 2, "the runner never restarted")
    bootstrap = SshHostBootstrap(transport=NodeTransport(node), resolver=None)

    removed = await bootstrap.quiesce(NODE, PROFILE, INTENT)

    assert removed >= 2
    restarts = node.restarts("runner")
    # Long enough for a missed restart to happen and leave another self-test.
    await asyncio.sleep(5)
    assert node.service("runner")["state"] == "exited"
    assert node.restarts("runner") == restarts
    assert node.probes() == []
    assert node.service("sandbox-manager")["state"] == "running"

    inventory = await bootstrap.inventory(NODE, PROFILE, INTENT)

    assert inventory.complete
    assert inventory.blocked_reasons == ()


@pytest.mark.asyncio
async def test_release_keeps_owner_sandboxes_and_other_deployments(node):
    _start_stack(node)
    _wait(lambda: len(node.probes()) >= 1, "the runner never created its self-test")
    owner = " ".join(_sandbox_labels(PROJECT))
    node.sh(f"docker run -d --name owner-sandbox {owner} {BASE} sleep 1d")
    elsewhere = " ".join(_probe_labels("ca-elsewhere"))
    node.sh(f"docker run -d --name other-self-test {elsewhere} {BASE} sleep 1d")
    node.sh(f"docker run -d --name unmanaged {BASE} sleep 1d")
    bootstrap = SshHostBootstrap(transport=NodeTransport(node), resolver=None)

    await bootstrap.quiesce(NODE, PROFILE, INTENT)

    names = {c["name"]: c["state"] for c in node.containers()}
    assert names["owner-sandbox"] == names["other-self-test"] == names["unmanaged"] == "running"
    assert node.probes() == []
    inventory = await bootstrap.inventory(NODE, PROFILE, INTENT)
    # Still found and still refused: quiescing excuses only its own self-tests.
    assert inventory.complete
    assert (inventory.retained_containers, inventory.unknown_containers) == (2, 1)
    assert set(inventory.blocked_reasons) == {
        "retained_containers_present",
        "unmanaged_containers_present",
    }


@pytest.mark.asyncio
async def test_inventory_reads_completely_while_containers_come_and_go(node):
    """A container removed between listing and reading it used to fail the whole read."""
    # Short-lived containers of the deployment's own project, so the only
    # thing the read can report is whether it completed.
    churn = (
        f"while true; do docker run --rm --label com.docker.compose.project={PROJECT} "
        f"{BASE} true >/dev/null 2>&1; done"
    )
    for _ in range(4):
        node.sh(f"nohup sh -c '{churn}' >/dev/null 2>&1 &")
    _wait(lambda: node.docker("ps", "-aq") != "", "no container churn started")
    bootstrap = SshHostBootstrap(transport=NodeTransport(node), resolver=None)

    reads = [await bootstrap.inventory(NODE, PROFILE, INTENT) for _ in range(30)]

    assert [read.blocked_reasons for read in reads if not read.complete] == []
