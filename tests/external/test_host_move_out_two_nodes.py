"""A Host's Docker state moves between two real nodes over SSH, and the old node empties.

Two disposable nodes stand in for CloudLab machines: Ubuntu with systemd as
PID 1, Docker from the packages the deploy installs, and sshd for the
management user (``tests/external/compute_node``). The control plane's own
``SshHostBootstrap`` reaches them through ``OpenSshTransport`` with pinned
host keys, exactly as it reaches a node in production, and runs the move-out
steps in the order the rotation runs them:

1. a first copy while the source still serves;
2. stop the source's stack and daemons;
3. the final copy from the stopped source;
4. start the target's daemons (the deploy does this) and check what arrived;
5. revoke the one-off transfer key, wipe the source and inventory it.

The source holds what a Host holds: a running chat sandbox with files in its
writable layer, a stopped container, a named volume and the Host's
enrollment state. Unit tests replace every one of these steps with a fake
that cannot get systemd, rsync, sshd's forced command or Docker's on-disk
state wrong.

Run by hand with ``pytest -m external tests/external/test_host_move_out_two_nodes.py``.
Needs Docker with ``--privileged``, cgroup v2 and the local image
``alpine:3.20`` (``AGENT_HOST_NODE_TEST_IMAGE`` overrides it); the node
image is built from ``tests/external/compute_node``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cloud_agent.ports.external.compute_provider import NodeObservation, SshConnection
from cloud_agent.ports.external.host_bootstrap import BootstrapIntent, DeploymentProfile
from cloud_agent.providers.compute.host_bootstrap import SshHostBootstrap
from cloud_agent.providers.compute.ssh import OpenSshTransport
from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.external

BASE = os.environ.get("AGENT_HOST_NODE_TEST_IMAGE", "alpine:3.20")
NODE_IMAGE = "cloud-agent-test-compute-node:local"
NODE_CONTEXT = Path(__file__).parent / "compute_node"
ROOT = "/opt/cloud-agent-move"
HOST = "managed-host-1"
PROFILE = DeploymentProfile(
    id="move",
    label="Move",
    source_bundle="/dev/null",
    source_sha256="0" * 64,
    deployment_sha="a" * 40,
    environment_ref="deployment:env",
    deploy_root=ROOT,
)
SOURCE_INTENT = BootstrapIntent(
    operation_id="operation-1", allocation_id="allocation-1", node_id="node-a", host=HOST
)
# The replacement keeps the Host's name and its sandbox namespace.
TARGET_INTENT = BootstrapIntent(
    operation_id="operation-2",
    allocation_id="allocation-2",
    node_id="node-b",
    host=HOST,
    namespace=SOURCE_INTENT.project,
)


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kwargs)


@dataclass
class Node:
    """One machine: reached by the test with ``docker exec``, by the control plane over SSH."""

    name: str
    address: str
    host_key: str

    def sh(self, script: str, *, check: bool = True) -> str:
        return _docker("exec", self.name, "sh", "-c", script, check=check).stdout

    def docker(self, *args: str, check: bool = True) -> str:
        return _docker("exec", self.name, "docker", *args, check=check).stdout

    def observation(self) -> NodeObservation:
        return NodeObservation(
            provider_node_id=self.name,
            instance_identity="allocation:" + self.name,
            display_name=self.name,
            machine_identity="verified",
            connection=SshConnection(
                address=self.address,
                username="agent",
                credential_ref="management-key",
                host_key=self.host_key,
            ),
        )


class Secrets:
    """The control plane's secret store, holding only the management key."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def resolve(self, ref: str) -> str:
        assert ref == "management-key"
        return self.key

    async def bundle(self, ref: str) -> bytes:
        raise AssertionError("moving a Host deploys no bundle here")


def _wait(condition, message: str, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(message)
        time.sleep(1)


@pytest.fixture(scope="module")
def node_image() -> str:
    if shutil.which("docker") is None or _docker("image", "inspect", BASE, check=False).returncode:
        pytest.skip(f"needs Docker and the local image {BASE}")
    if shutil.which("ssh") is None:
        pytest.skip("needs an OpenSSH client")
    _docker("build", "-q", "-t", NODE_IMAGE, str(NODE_CONTEXT))
    return NODE_IMAGE


@pytest.fixture
def management_key(tmp_path: Path) -> tuple[str, str]:
    path = tmp_path / "management"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "control-plane", "-f", str(path)],
        check=True,
    )
    return path.read_text(), (tmp_path / "management.pub").read_text().strip()


@pytest.fixture
def nodes(node_image: str, management_key: tuple[str, str]) -> Iterator[tuple[Node, Node]]:
    suffix = secrets.token_hex(4)
    network = f"ca-move-{suffix}"
    names = [f"ca-move-{role}-{suffix}" for role in ("source", "target")]
    _docker("network", "create", network)
    try:
        for name in names:
            # Docker's and containerd's state on volumes of their own, as on
            # a node's data disk: overlay mounts cannot nest on overlay.
            _docker(
                "run",
                "-d",
                "--privileged",
                "--cgroupns=host",
                "-v",
                "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                "--mount",
                "type=volume,dst=/var/lib/docker",
                "--mount",
                "type=volume,dst=/var/lib/containerd",
                "--network",
                network,
                "--hostname",
                name,
                "--name",
                name,
                node_image,
            )
        built: list[Node] = []
        for name in names:
            _wait(
                lambda name=name: (
                    _docker(
                        "exec", name, "systemctl", "is-active", "docker", "ssh", check=False
                    ).returncode
                    == 0
                ),
                f"{name} did not boot",
            )
            _docker(
                "exec",
                name,
                "sh",
                "-c",
                "install -d -m 700 -o agent -g agent /home/agent/.ssh && "
                f"echo '{management_key[1]}' > /home/agent/.ssh/authorized_keys && "
                "chown agent:agent /home/agent/.ssh/authorized_keys && "
                "chmod 600 /home/agent/.ssh/authorized_keys",
            )
            address = _docker(
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                name,
            ).stdout.strip()
            host_key = " ".join(
                _docker("exec", name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub").stdout.split()[:2]
            )
            built.append(Node(name, address, host_key))
        yield built[0], built[1]
    finally:
        for name in names:
            _docker("rm", "--force", "--volumes", name, check=False)
        _docker("network", "rm", network, check=False)


def _write_marker(node: Node, intent: BootstrapIntent) -> None:
    """The deployment identity a deploy (or a stage) leaves on the node."""
    identity = SshHostBootstrap._params(PROFILE, intent)["identity"]
    node.sh(
        f"mkdir -p {ROOT} && printf '%s' '{json.dumps(identity)}' > {ROOT}/.compute-identity.json"
    )


def _sandbox_labels(namespace: str) -> str:
    manager = ChatSandboxManager(ContainerBackend(image=BASE, sandbox_namespace=namespace))
    spec = ChatSandboxSpec(
        thread_id="athr_moved", sandbox_id="asbx_moved", generation=1, runtime_id="claude-code"
    )
    command = manager.build_create_command(spec)
    return " ".join(f"--label {value}" for flag, value in pairwise(command) if flag == "--label")


def _populate_source(source: Node) -> dict[str, str]:
    """What a serving Host holds, with the IDs the target must keep."""
    image = subprocess.run(["docker", "save", BASE], capture_output=True, check=True).stdout
    subprocess.run(
        ["docker", "exec", "-i", source.name, "docker", "load", "-q"],
        input=image,
        capture_output=True,
        check=True,
    )
    labels = _sandbox_labels(SOURCE_INTENT.project)
    chat = source.docker(
        "run",
        "-d",
        "--name",
        "chat",
        *labels.split(),
        BASE,
        "sh",
        "-c",
        "echo 'work in progress' > /workspace.txt && exec sleep 1d",
    ).strip()
    _wait(
        lambda: source.docker("exec", "chat", "cat", "/workspace.txt", check=False) != "",
        "the chat sandbox did not write its file",
    )
    stopped = source.docker("create", "--name", "stopped", BASE, "true").strip()
    source.docker("create", "--name", "discarded", BASE, "true")
    source.docker("volume", "create", "memory")
    source.docker(
        "run", "--rm", "-v", "memory:/memory", BASE, "sh", "-c", "echo kept > /memory/note"
    )
    source.sh(
        f"mkdir -p /var/lib/cloud-agent/{HOST} && echo token > /var/lib/cloud-agent/{HOST}/credential"
    )
    return {"chat": chat, "stopped": stopped}


@pytest.mark.asyncio
async def test_a_host_moves_to_its_replacement_and_the_old_node_empties(nodes, management_key):
    source, target = nodes
    _write_marker(source, SOURCE_INTENT)
    _write_marker(target, TARGET_INTENT)
    ids = _populate_source(source)
    bootstrap = SshHostBootstrap(
        transport=OpenSshTransport(Secrets(management_key[0])), resolver=Secrets(management_key[0])
    )
    source_node, target_node = source.observation(), target.observation()

    # 1. While the source serves.
    first = await bootstrap.transfer(
        source_node, target_node, PROFILE, TARGET_INTENT, final=False, first=True
    )
    assert first.bytes_transferred > 0
    # The Host keeps serving: one sandbox goes away and another arrives.
    source.docker("rm", "discarded")
    ids["late"] = source.docker("create", "--name", "late", BASE, "true").strip()
    # 2. Claims paused: the source's stack and daemons stop.
    await bootstrap.stop(source_node, PROFILE, SOURCE_INTENT)
    assert source.sh("systemctl is-active docker containerd", check=False).split() == [
        "inactive",
        "inactive",
    ]
    # 3. The last changes, from the stopped source.
    await bootstrap.transfer(source_node, target_node, PROFILE, TARGET_INTENT, final=True)

    # 4. The target starts on the copied state, the same containers and data.
    target.sh("systemctl start containerd docker")
    moved = dict(
        line.split("\t")[::-1]
        for line in target.docker(
            "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"
        ).splitlines()
    )
    # Exactly the source as it stopped: nothing from before the first copy
    # that was removed since, nothing missing that arrived after it.
    assert moved == ids
    target.docker("start", "chat")
    assert target.docker("exec", "chat", "cat", "/workspace.txt") == "work in progress\n"
    assert (
        target.docker("run", "--rm", "-v", "memory:/memory", BASE, "cat", "/memory/note")
        == "kept\n"
    )
    labels = json.loads(target.docker("inspect", "--format", "{{json .Config.Labels}}", "chat"))
    assert labels["org.freeinference.sandbox-namespace"] == TARGET_INTENT.project

    # 5. The source gives the key back and empties; release then finds nothing.
    await bootstrap.revoke_transfer(source_node, target_node)
    assert "ca-transfer" not in target.sh("cat /home/agent/.ssh/authorized_keys")
    await bootstrap.wipe(source_node, PROFILE, SOURCE_INTENT)
    assert source.docker("ps", "-aq") == ""
    assert source.docker("images", "-q") == ""
    assert source.sh(f"test -e /var/lib/cloud-agent/{HOST} && echo left", check=False) == ""
    inventory = await bootstrap.inventory(source_node, PROFILE, SOURCE_INTENT)
    assert inventory.complete
    assert inventory.blocked_reasons == ()
