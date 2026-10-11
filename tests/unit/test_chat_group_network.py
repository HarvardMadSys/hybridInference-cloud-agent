"""The group network: one guarded bridge per sandbox group, against a model of Docker.

Setup, the agent and every fork member use one routable bridge named from the
group's identity. The host's rules (``cloud_agent_shared.egress``) decide what
it reaches; this lifecycle keeps the bridge, its two relays and the host's
per-bridge relay exceptions consistent through create, wake, stop, relay
rollouts and pruning, and refuses the per-phase topology it replaced.

The Docker model below answers the commands the manager actually sends, so the
real reconciliation code runs end to end. Behaviour the model cannot show (a
kernel enforcing the rules) is the external tier's.
"""

from __future__ import annotations

import contextlib
import errno
import inspect
import io
import ipaddress
import itertools
import json
import logging
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host.chat_sandbox import (
    _GUEST_NETWORK_CHECK,
    CHAT_BOOTSTRAP_NETWORK,
    CHAT_NAMESPACE_LABEL,
    CHAT_NETWORK_BRIDGE_OPTION,
    CHAT_NETWORK_LABEL,
    CHAT_NETWORK_SERVICES_LABEL,
    CHAT_NULL_NETWORK,
    CHAT_OFFLINE_DNS,
    RELAY_GATEWAY_PRIORITY,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatFailStopped,
    RetainedChatLost,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxError
from cloud_agent_shared.egress import SandboxNetwork, network_from_env

_NAMESPACE = "this-stack"


def _config(namespace: str = _NAMESPACE) -> SandboxNetwork:
    return network_from_env(
        {
            "AGENT_SANDBOX_NAMESPACE": namespace,
            "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
            "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
            "AGENT_RELAY_NETWORK": "agent-relays",
            "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
            "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
        }
    )


def _spec(
    thread: str = "athr_01", sandbox: str = "asbx_01", generation: int = 3
) -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id=thread,
        sandbox_id=sandbox,
        generation=generation,
        runtime_id="claude-code",
        sandbox_image_digest="sha256:image-pinned",
    )


class FakeGuard:
    """Stands in for the host guard: records what the lifecycle asks of it."""

    def __init__(self) -> None:
        self.ensured = 0
        self.fail: Exception | None = None
        self.relays: dict[str, set[tuple[str, int]]] = {}
        self.events: list[str] = []

    def ensure(self, **_kwargs: Any) -> None:
        self.events.append("ensure")
        if self.fail is not None:
            raise self.fail
        self.ensured += 1

    def sync_relays(self, bridge: str, endpoints: set[tuple[str, int]]) -> None:
        self.events.append("sync_relays")
        self.relays[bridge] = set(endpoints)


class FakeDocker:
    """Networks, endpoints and containers, answering the manager's commands."""

    def __init__(self, config: SandboxNetwork) -> None:
        self.config = config
        self.networks: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        #: Runs just before a ``network rm``: how a test makes a sandbox win
        #: the race and attach after the cleanup's last inspection.
        self.before_rm: Any = None
        #: False models a Docker older than 28, which drops ``--gw-priority``.
        self.honours_gw_priority = True
        self._ids = itertools.count(1)
        relays = self.add_network(config.relay_network, subnet="172.30.0.0/24", internal=True)
        for container_id, alias in (("relay-gateway", "agent-gateway"), ("relay-mcp", "agent-mcp")):
            self.add_container(container_id, running=True)
            self.connect(relays["Id"], container_id, [alias])

    # ── state helpers ─────────────────────────────────────────────────

    def add_network(
        self,
        name: str,
        *,
        subnet: str,
        internal: bool = False,
        options: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        age_s: float = 0,
        ipv6: bool = False,
    ) -> dict[str, Any]:
        network_id = f"net-{next(self._ids)}"
        created = datetime.now(UTC) - timedelta(seconds=age_s)
        self.networks[network_id] = {
            "Id": network_id,
            "Name": name,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": internal,
            "EnableIPv6": ipv6,
            "Options": dict(options or {}),
            "Labels": dict(labels or {}),
            "IPAM": {"Config": [{"Subnet": subnet}]},
            "Created": created.isoformat().replace("+00:00", "Z"),
            "endpoints": {},
        }
        return self.networks[network_id]

    def add_container(
        self,
        container_id: str,
        *,
        name: str | None = None,
        running: bool = False,
        status: str | None = None,
        labels: dict[str, str] | None = None,
        image: str = "sha256:image-one",
    ) -> dict[str, Any]:
        self.containers[container_id] = {
            "Id": container_id,
            "Name": "/" + (name or container_id),
            "running": running,
            "status": status or ("running" if running else "exited"),
            "labels": dict(labels or {}),
            "image": image,
            "networks": {},
        }
        return self.containers[container_id]

    def add_sandbox(self, spec: ChatSandboxSpec, **kwargs: Any) -> dict[str, Any]:
        labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: _NAMESPACE}
        labels.update(kwargs.pop("labels", {}))
        return self.add_container(
            "sandbox-container",
            name=spec.container_name,
            labels=labels,
            image=spec.sandbox_image_digest or "img",
            **kwargs,
        )

    def network(self, ref: str) -> dict[str, Any] | None:
        for network in self.networks.values():
            if ref in (network["Id"], network["Name"]):
                return network
        return None

    def container(self, ref: str) -> dict[str, Any] | None:
        for container in self.containers.values():
            if ref in (container["Id"], container["Name"].removeprefix("/")) or (
                len(ref) >= 6 and container["Id"].startswith(ref)
            ):
                return container
        return None

    def connect(
        self, network_id: str, container_id: str, aliases: list[str], priority: int = 0
    ) -> str:
        network = self.networks[network_id]
        container = self.containers[container_id]
        subnet = ipaddress.IPv4Network(network["IPAM"]["Config"][0]["Subnet"])
        taken = {endpoint["ip"] for endpoint in network["endpoints"].values()}
        address = next(str(host) for host in list(subnet.hosts())[1:] if str(host) not in taken)
        network["endpoints"][container_id] = {"ip": address, "aliases": list(aliases)}
        container["networks"][network["Name"]] = {
            # Docker fills a stopped container's pending endpoint on start.
            "NetworkID": network_id if container["running"] else "",
            "IPAddress": address,
            "Aliases": list(aliases),
            "DNSNames": [*aliases, container["Name"].removeprefix("/")],
            "GwPriority": priority,
        }
        return address

    def names_on(self, network_name: str) -> set[str]:
        network = self.network(network_name)
        return set((network or {}).get("endpoints", {}))

    # ── the Docker CLI ────────────────────────────────────────────────

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        command = argv[1:3]
        if command == ["network", "inspect"]:
            network = self.network(argv[3])
            if network is None:
                return self._fail("Error response from daemon: network not found")
            return self._ok(json.dumps([self._network_json(network)]))
        if command == ["network", "create"]:
            return self._create_network(argv)
        if command == ["network", "connect"]:
            aliases = [argv[i + 1] for i, value in enumerate(argv) if value == "--alias"]
            network, container = self.network(argv[-2]), self.container(argv[-1])
            if network is None or container is None:
                return self._fail("no such network or container")
            if container["Id"] in network["endpoints"]:
                return self._fail("endpoint already exists in network")
            priority = 0
            if "--gw-priority" in argv and self.honours_gw_priority:
                priority = int(argv[argv.index("--gw-priority") + 1])
            self.connect(network["Id"], container["Id"], aliases, priority)
            return self._ok("")
        if command == ["network", "disconnect"]:
            network_ref, container_ref = argv[-2], argv[-1]
            container = self.container(container_ref)
            network = self.network(network_ref)
            if container is None:
                return self._fail("no such container")
            if network is not None:
                network["endpoints"].pop(container["Id"], None)
                container["networks"].pop(network["Name"], None)
            else:
                # A stale record for a bridge that is already gone.
                container["networks"].pop(network_ref, None)
            return self._ok("")
        if command == ["network", "rm"]:
            if self.before_rm is not None:
                self.before_rm()
                self.before_rm = None
            network = self.network(argv[3])
            if network is None:
                return self._fail("network not found")
            if network["endpoints"]:
                return self._fail("network has active endpoints")
            del self.networks[network["Id"]]
            return self._ok("")
        if command == ["network", "ls"]:
            label = argv[argv.index("--filter") + 1].removeprefix("label=")
            key, _, value = label.partition("=")
            return self._ok(
                "\n".join(
                    network["Id"]
                    for network in self.networks.values()
                    if network["Labels"].get(key) == value
                )
            )
        if command == ["container", "inspect"]:
            found = [self.container(ref) for ref in argv[3:]]
            payload = [self._container_json(container) for container in found if container]
            if len(payload) != len(argv[3:]):
                return subprocess.CompletedProcess(argv, 1, json.dumps(payload), "No such object")
            return self._ok(json.dumps(payload))
        if argv[1] == "start":
            container = self.container(argv[2])
            assert container is not None
            container["running"], container["status"] = True, "running"
            for name, endpoint in container["networks"].items():
                network = self.network(name)
                endpoint["NetworkID"] = network["Id"] if network else endpoint["NetworkID"]
            return self._ok("")
        if argv[1] == "stop":
            container = self.container(argv[-1])
            assert container is not None
            container["running"], container["status"] = False, "exited"
            return self._ok("")
        raise AssertionError(f"unmodelled docker command: {argv}")

    def _create_network(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        name = argv[-1]
        if self.network(name) is not None:
            return self._fail("network with name already exists")
        subnet = argv[argv.index("--subnet") + 1]
        wanted = ipaddress.IPv4Network(subnet)
        for network in self.networks.values():
            existing = ipaddress.IPv4Network(network["IPAM"]["Config"][0]["Subnet"])
            if existing.overlaps(wanted):
                return self._fail("Pool overlaps with other one on this address space")
        options = dict(
            argv[i + 1].split("=", 1) for i, value in enumerate(argv) if value == "--opt"
        )
        labels = dict(
            argv[i + 1].split("=", 1) for i, value in enumerate(argv) if value == "--label"
        )
        self.add_network(
            name,
            subnet=subnet,
            internal="--internal" in argv,
            options=options,
            labels=labels,
        )
        return self._ok(name)

    def _network_json(self, network: dict[str, Any]) -> dict[str, Any]:
        length = network["IPAM"]["Config"][0]["Subnet"].split("/")[1]
        return {
            **{key: value for key, value in network.items() if key != "endpoints"},
            "Containers": {
                container_id: {"IPv4Address": f"{endpoint['ip']}/{length}"}
                for container_id, endpoint in network["endpoints"].items()
            },
        }

    def _container_json(self, container: dict[str, Any]) -> dict[str, Any]:
        return {
            "Id": container["Id"],
            "Name": container["Name"],
            "Image": "sha256:local",
            "Config": {
                "Image": container["image"],
                "Labels": container["labels"],
                "Hostname": container["Id"][:12],
            },
            "State": {
                "Status": container["status"],
                "Running": container["running"],
                "Paused": False,
            },
            "HostConfig": {"Runtime": ""},
            "Mounts": [],
            "NetworkSettings": {"Networks": container["networks"]},
        }

    @staticmethod
    def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout, "")

    @staticmethod
    def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", stderr)


def _world(monkeypatch, *, runtime: str | None = None):
    config = _config()
    backend = ContainerBackend(
        image="img", runtime=runtime, sandbox_network=config, sandbox_namespace=_NAMESPACE
    )
    guard = FakeGuard()
    backend._network_guard = guard  # type: ignore[assignment]
    manager = ChatSandboxManager(backend)
    docker = FakeDocker(config)
    monkeypatch.setattr(manager, "_run", docker)
    return manager, docker, guard


def _owned_labels(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> dict[str, str]:
    return manager._network_labels(spec)


def _group_bridge(manager, docker, spec, *, age_s: float = 0, subnet: str = "10.240.9.0/29"):
    name = manager.network_for(spec)
    network = docker.add_network(
        name,
        subnet=subnet,
        options={CHAT_NETWORK_BRIDGE_OPTION: manager._network().bridge_name(name)},
        labels=_owned_labels(manager, spec),
        age_s=age_s,
    )
    return name, network


# ── naming ─────────────────────────────────────────────────────────────


def test_each_group_gets_one_bridge_named_from_its_identity(monkeypatch):
    manager, _, _ = _world(monkeypatch)
    first = _spec()
    name = manager.network_for(first)
    assert name.startswith("freeinference-chat-net-")
    assert name == manager.network_for(first), "setup, agent and fork members share it"
    assert name != manager.network_for(_spec(thread="athr_02", sandbox="asbx_02"))
    assert name != manager.network_for(_spec(generation=4)), "a new generation, a new bridge"
    bridge = manager._network().bridge_name(name)
    assert len(bridge) <= 15 and bridge.startswith(manager._network().interface_prefix)


def test_there_is_no_phase_switch_left_to_call(monkeypatch):
    manager, _, _ = _world(monkeypatch)
    assert not hasattr(manager, "transition_to_agent_network")
    assert not hasattr(manager, "require_network_phase")
    assert "network_phase" not in inspect.signature(manager.start).parameters
    assert "network_phase" not in inspect.signature(manager.create).parameters


def test_the_sandbox_resolves_through_the_deployments_one_resolver(monkeypatch):
    manager, _, _ = _world(monkeypatch)
    command = manager.build_create_command(_spec())
    assert command[command.index("--network") + 1] == CHAT_BOOTSTRAP_NETWORK
    assert command[command.index("--dns") + 1] == "1.1.1.1"
    offline = ChatSandboxManager(ContainerBackend(image="img"))
    command = offline.build_create_command(_spec())
    assert command[command.index("--dns") + 1] == CHAT_OFFLINE_DNS


# ── creating and reconciling the bridge ───────────────────────────────


def test_a_new_bridge_is_routable_named_for_the_host_rules_and_holds_only_its_relays(
    monkeypatch,
):
    manager, docker, guard = _world(monkeypatch)
    spec = _spec()

    name = manager._ensure_group_network(spec)

    create = next(argv for argv in docker.calls if argv[1:3] == ["network", "create"])
    assert "--internal" not in create, "routable: the host's rules decide the reach"
    bridge = manager._network().bridge_name(name)
    assert f"{CHAT_NETWORK_BRIDGE_OPTION}={bridge}" in create
    subnet = ipaddress.IPv4Network(create[create.index("--subnet") + 1])
    assert subnet.prefixlen == 29 and subnet.subnet_of(ipaddress.IPv4Network("10.240.0.0/16"))
    for key, value in _owned_labels(manager, spec).items():
        assert f"{key}={value}" in create
    assert f"{CHAT_NETWORK_SERVICES_LABEL}=agent-gateway,agent-mcp" in create
    assert docker.names_on(name) == {"relay-gateway", "relay-mcp"}
    # The host holds exactly one exception per relay, on this bridge's name.
    endpoints = docker.network(name)["endpoints"]
    assert guard.relays[bridge] == {
        (endpoints["relay-gateway"]["ip"], 8080),
        (endpoints["relay-mcp"]["ip"], 8000),
    }
    assert guard.events[0] == "ensure", "rules are proved before any bridge exists"


def test_relays_join_a_group_bridge_that_can_never_become_their_way_out(monkeypatch):
    """Their SSH upstream stays on the stack's own network, not the sandbox rules."""
    manager, docker, _ = _world(monkeypatch)

    name = manager._ensure_group_network(_spec())

    connects = [argv for argv in docker.calls if argv[1:3] == ["network", "connect"]]
    assert connects and all(
        argv[argv.index("--gw-priority") + 1] == str(RELAY_GATEWAY_PRIORITY) for argv in connects
    )
    assert RELAY_GATEWAY_PRIORITY < 0
    for relay in ("relay-gateway", "relay-mcp"):
        assert docker.container(relay)["networks"][name]["GwPriority"] < 0


def test_a_relay_attached_without_the_priority_is_attached_again_with_it(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    name, network = _group_bridge(manager, docker, spec)
    docker.connect(network["Id"], "relay-gateway", ["agent-gateway"])  # priority 0

    manager._ensure_group_network(spec)

    assert ["docker", "network", "disconnect", "--force", name, "relay-gateway"] in docker.calls
    assert docker.container("relay-gateway")["networks"][name]["GwPriority"] < 0


def test_a_docker_that_ignores_the_gateway_priority_is_refused(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    docker.honours_gw_priority = False
    with pytest.raises(SandboxError, match="gateway priority"):
        manager._ensure_group_network(_spec())


def test_no_bridge_is_built_on_a_host_whose_rules_fail(monkeypatch):
    manager, docker, guard = _world(monkeypatch)
    guard.fail = SandboxError("inet cloud_agent_x chain 'from_group' holds 0 rules")
    with pytest.raises(SandboxError, match="from_group"):
        manager._ensure_group_network(_spec())
    assert not [argv for argv in docker.calls if argv[1:3] == ["network", "create"]]


def test_the_allocator_moves_past_a_subnet_another_process_claimed(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    first_choice = next(manager._candidate_chat_subnets(manager.network_for(spec)))
    docker.add_network("someone-else", subnet=str(first_choice))

    name = manager._ensure_group_network(spec)

    creates = [argv for argv in docker.calls if argv[1:3] == ["network", "create"]]
    assert len(creates) == 2
    assert docker.network(name)["IPAM"]["Config"][0]["Subnet"] != str(first_choice)


@pytest.mark.parametrize(
    "change",
    [
        {"internal": True},
        {"ipv6": True},
        {"options": {}},
        {"options": {CHAT_NETWORK_BRIDGE_OPTION: "docker0"}},
        {"labels": {}},
    ],
    ids=["internal-legacy", "ipv6", "unnamed-bridge", "foreign-bridge", "unlabelled"],
)
def test_a_bridge_with_the_group_name_but_not_the_guarded_shape_is_refused(monkeypatch, change):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    name = manager.network_for(spec)
    shape = {
        "subnet": "10.240.9.0/29",
        "options": {CHAT_NETWORK_BRIDGE_OPTION: manager._network().bridge_name(name)},
        "labels": _owned_labels(manager, spec),
    }
    shape.update(change)
    docker.add_network(name, **shape)

    with pytest.raises(SandboxError, match="guarded group bridge"):
        manager._ensure_group_network(spec)


def test_an_unexpected_peer_on_a_group_bridge_fails_closed(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    _, network = _group_bridge(manager, docker, spec)
    docker.add_container("stranger", running=True)
    docker.connect(network["Id"], "stranger", ["something-else"])

    with pytest.raises(SandboxError, match="unexpected peer"):
        manager._ensure_group_network(spec)


def test_a_relay_rollout_swaps_the_endpoint_and_the_host_exception(monkeypatch):
    manager, docker, guard = _world(monkeypatch)
    spec = _spec()
    name = manager._ensure_group_network(spec)
    bridge = manager._network().bridge_name(name)
    old_ip = docker.network(name)["endpoints"]["relay-gateway"]["ip"]

    # Compose replaces the gateway relay: the old container leaves the relay
    # network and a new one answers to the same alias.
    relays = docker.network("agent-relays")
    relays["endpoints"].pop("relay-gateway")
    docker.containers["relay-gateway"]["running"] = False
    docker.add_container("relay-gateway-2", running=True)
    docker.connect(relays["Id"], "relay-gateway-2", ["agent-gateway"])

    manager._ensure_group_network(spec)

    endpoints = docker.network(name)["endpoints"]
    assert "relay-gateway" not in endpoints and "relay-gateway-2" in endpoints
    new_ip = endpoints["relay-gateway-2"]["ip"]
    assert (new_ip, 8080) in guard.relays[bridge]
    assert (old_ip, 8080) not in guard.relays[bridge] or old_ip == new_ip


def test_relay_owners_are_read_in_one_inspect_and_must_be_unambiguous(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    assert manager._relay_owners() == {
        "relay-gateway": ["agent-gateway"],
        "relay-mcp": ["agent-mcp"],
    }
    assert sum(argv[1:3] == ["container", "inspect"] for argv in docker.calls) == 1
    docker.add_container("second-gateway", running=True)
    docker.connect(docker.network("agent-relays")["Id"], "second-gateway", ["agent-gateway"])
    with pytest.raises(SandboxError, match="multiple running containers"):
        manager._relay_owners()


# ── attaching the sandbox ─────────────────────────────────────────────


def test_first_activation_moves_the_sandbox_off_the_dormant_bootstrap_bridge(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec, status="created")
    bootstrap = docker.add_network(CHAT_BOOTSTRAP_NETWORK, subnet="172.17.0.0/16")
    docker.connect(bootstrap["Id"], "sandbox-container", [])

    moved = manager._require_group_network(spec, manager._require(spec))

    assert moved.networks == (manager.network_for(spec),)


def test_a_running_sandbox_is_never_rewired_underneath_it(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec, running=True)
    elsewhere = docker.add_network("some-bridge", subnet="172.18.0.0/16")
    docker.connect(elsewhere["Id"], "sandbox-container", [])

    with pytest.raises(SandboxError):
        manager._require_group_network(spec, manager._require(spec))
    assert "sandbox-container" in docker.network("some-bridge")["endpoints"]


def test_a_sandbox_left_on_the_old_per_phase_topology_is_refused_not_adopted(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec)
    legacy = docker.add_network(
        "freeinference-chat-net-oldphase",
        subnet="10.240.200.0/29",
        internal=True,
        labels={
            CHAT_NETWORK_LABEL: "true",
            CHAT_NAMESPACE_LABEL: _NAMESPACE,
            "org.freeinference.chat-sandbox-id": spec.sandbox_id,
            "org.freeinference.chat-generation": str(spec.generation),
        },
    )
    docker.connect(legacy["Id"], "sandbox-container", [])

    with pytest.raises(SandboxError, match="one-time cleanup"):
        manager._require_group_network(spec, manager._require(spec))
    with pytest.raises(SandboxError, match="one-time cleanup"):
        manager.stop(spec)
    assert docker.network("freeinference-chat-net-oldphase") is not None


def test_idle_stop_releases_the_bridge_and_its_exceptions_and_wake_rebuilds(monkeypatch):
    manager, docker, guard = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec, running=True)
    name = manager._ensure_group_network(spec)
    docker.connect(docker.network(name)["Id"], "sandbox-container", [])
    bridge = manager._network().bridge_name(name)
    checked: list[str] = []
    monkeypatch.setattr(
        manager,
        "_verify_guest_network",
        lambda s, **_kwargs: checked.append(s.sandbox_id),
    )

    stopped = manager.stop(spec)
    assert docker.network(name) is None
    assert stopped.networks == ()
    assert guard.relays[bridge] == set()

    started = manager.start(spec)
    assert started.running is True
    assert started.networks == (name,)
    assert docker.names_on(name) == {"relay-gateway", "relay-mcp", "sandbox-container"}
    assert guard.relays[bridge] and checked == [spec.sandbox_id]


def test_wake_reconnects_a_same_name_bridge_when_docker_kept_the_old_id(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec)
    name, network = _group_bridge(manager, docker, spec)
    docker.connect(network["Id"], "sandbox-container", [])
    # The bridge was removed and recreated under the same name; the stopped
    # container still carries the dead bridge's id.
    docker.containers["sandbox-container"]["networks"][name]["NetworkID"] = "deleted-network-id"
    network["endpoints"].pop("sandbox-container")

    refreshed = manager._require_group_network(spec, manager._require(spec))

    assert ["docker", "network", "connect", name, "sandbox-container"] in docker.calls
    assert refreshed.networks == (name,)


def test_start_proves_the_network_from_inside_and_stops_a_sandbox_that_fails(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec)
    problems = json.dumps(["relay agent-mcp:8000 unreachable: timed out"]).encode()
    monkeypatch.setattr(
        manager,
        "exec_capture",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, problems, b""),
    )

    with pytest.raises(RetainedChatFailStopped, match="agent-mcp:8000 unreachable") as failed:
        manager.start(spec)
    # Stopped, its bridge released, its workspace (the container) kept.
    assert failed.value.inspection.running is False
    assert failed.value.inspection.container_id == "sandbox-container"
    assert docker.containers["sandbox-container"]["running"] is False
    assert docker.network(manager.network_for(spec)) is None


def test_the_guest_check_expects_the_resolver_each_runtime_really_uses(monkeypatch):
    seen: list[list[str]] = []

    def capture(_spec, argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")

    for runtime, resolver in ((None, "127.0.0.11"), (KATA_RUNTIME, "1.1.1.1")):
        manager, docker, _ = _world(monkeypatch, runtime=runtime)
        docker.add_sandbox(_spec(), running=True)
        seen.clear()
        monkeypatch.setattr(manager, "exec_capture", capture)
        manager._verify_guest_network(_spec())
        assert seen[0][3] == resolver
        assert json.loads(seen[0][4]) == [["agent-gateway", 8080], ["agent-mcp", 8000]]


def test_a_kata_guest_is_given_its_relays_and_the_one_resolver(monkeypatch):
    manager, docker, _ = _world(monkeypatch, runtime=KATA_RUNTIME)
    spec = _spec()
    docker.add_sandbox(spec, running=True)
    name = manager._ensure_group_network(spec)
    docker.connect(docker.network(name)["Id"], "sandbox-container", [])
    captured: list[list[str]] = []
    monkeypatch.setattr(
        manager,
        "_kata_privileged_exec",
        lambda _spec, argv, **_kwargs: (
            captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )

    manager._sync_kata_guest_network(spec, manager._require(spec))

    argv = captured[0]
    endpoints = docker.network(name)["endpoints"]
    assert json.loads(argv[3]) == {
        "agent-gateway": endpoints["relay-gateway"]["ip"],
        "agent-mcp": endpoints["relay-mcp"]["ip"],
    }
    assert argv[4] == "1.1.1.1"
    assert "/etc/resolv.conf" in argv[2] and "O_NOFOLLOW" in argv[2]


def _running_kata_group(monkeypatch):
    manager, docker, guard = _world(monkeypatch, runtime=KATA_RUNTIME)
    spec = _spec()
    docker.add_sandbox(spec, running=True)
    name = manager._ensure_group_network(spec)
    network = docker.network(name)
    network["Created"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    docker.connect(network["Id"], "sandbox-container", [])
    synced: list[dict[str, str]] = []
    monkeypatch.setattr(
        manager,
        "_kata_privileged_exec",
        lambda _spec, argv, **_kwargs: (
            synced.append(json.loads(argv[3])) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )
    docker.calls.clear()
    return manager, docker, guard, spec, name, synced


@pytest.mark.parametrize("operation", ["require_network", "start"])
def test_hot_network_reconciliation_reuses_only_its_own_inspections(monkeypatch, operation):
    manager, docker, guard, spec, name, synced = _running_kata_group(monkeypatch)
    checked = guard.ensured
    for attempt in range(2):
        docker.calls.clear()
        getattr(manager, operation)(spec)
        inspections = [
            argv
            for argv in docker.calls
            if argv[1:3] in (["network", "inspect"], ["container", "inspect"])
        ]
        # Previously 19 inspections per call, before the guest helper's own
        # identity check. Reusing data inside one call must not become a cache
        # across commands: the second call gathers the same fresh evidence.
        assert len(inspections) == 10
        assert ["docker", "container", "inspect", "relay-gateway"] not in inspections
        assert ["docker", "container", "inspect", "relay-mcp"] not in inspections
        assert not any(
            argv[1:3] in (["network", "connect"], ["network", "disconnect"])
            for argv in docker.calls
        )
        assert guard.ensured == checked + attempt + 1
    endpoints = docker.network(name)["endpoints"]
    expected = {
        "agent-gateway": endpoints["relay-gateway"]["ip"],
        "agent-mcp": endpoints["relay-mcp"]["ip"],
    }
    assert synced == [expected, expected], "guest file protections are checked on every call"


def test_a_later_command_observes_new_relay_owners_and_guest_addresses(monkeypatch):
    manager, docker, guard, spec, name, synced = _running_kata_group(monkeypatch)
    manager.require_network(spec)
    relays = docker.network("agent-relays")
    relays["endpoints"].pop("relay-gateway")
    docker.containers["relay-gateway"]["running"] = False
    docker.add_container("relay-gateway-new", running=True)
    docker.connect(relays["Id"], "relay-gateway-new", ["agent-gateway"])

    manager.require_network(spec)

    endpoints = docker.network(name)["endpoints"]
    assert "relay-gateway" not in endpoints
    assert synced[-1]["agent-gateway"] == endpoints["relay-gateway-new"]["ip"]
    assert guard.relays[manager._network().bridge_name(name)] == {
        (synced[-1]["agent-gateway"], 8080),
        (synced[-1]["agent-mcp"], 8000),
    }
    # The next command must also notice a new untrusted peer even though the
    # previous reconciliation passed for this exact sandbox generation.
    docker.add_container("stranger", running=True)
    docker.connect(docker.network(name)["Id"], "stranger", ["untrusted"])
    with pytest.raises(SandboxError, match="unexpected peer"):
        manager.require_network(spec)
    assert len(synced) == 2


@pytest.mark.parametrize(
    "mutation", ["peer", "relay_address", "bridge_id", "extra_network", "sandbox_endpoint"]
)
def test_activation_refuses_topology_changes_after_the_rule_sync(monkeypatch, mutation):
    manager, docker, guard, spec, name, synced = _running_kata_group(monkeypatch)
    sync = guard.sync_relays

    def mutate_after_sync(bridge, endpoints):
        sync(bridge, endpoints)
        network = docker.network(name)
        if mutation == "peer":
            docker.add_container("stranger", running=True)
            docker.connect(network["Id"], "stranger", ["untrusted"])
        elif mutation == "relay_address":
            network["endpoints"]["relay-gateway"]["ip"] = "10.240.99.6"
        elif mutation == "bridge_id":
            network["Id"] = "replacement-with-the-same-name"
        elif mutation == "sandbox_endpoint":
            network["endpoints"].pop("sandbox-container")
        else:
            foreign = docker.add_network("unguarded", subnet="172.19.0.0/24")
            docker.connect(foreign["Id"], "sandbox-container", [])

    monkeypatch.setattr(guard, "sync_relays", mutate_after_sync)
    with pytest.raises(SandboxError, match="changed during activation"):
        manager.start(spec)
    assert synced == [], "a failed snapshot must never reach the privileged guest helper"


def test_relay_replacement_during_reconciliation_refuses_old_addresses(monkeypatch):
    manager, docker, guard, spec, _name, synced = _running_kata_group(monkeypatch)
    discoveries = 0
    previous_syncs = guard.events.count("sync_relays")

    def replace_during_discovery(argv, **kwargs):
        nonlocal discoveries
        if argv[1:] == ["network", "inspect", "agent-relays"]:
            discoveries += 1
            if discoveries == 2:
                relays = docker.network("agent-relays")
                relays["endpoints"].pop("relay-gateway")
                docker.containers["relay-gateway"]["running"] = False
                docker.add_container("relay-gateway-new", running=True)
                docker.connect(relays["Id"], "relay-gateway-new", ["agent-gateway"])
        return docker(argv, **kwargs)

    monkeypatch.setattr(manager, "_run", replace_during_discovery)
    with pytest.raises(SandboxError, match="relay owners changed"):
        manager.require_network(spec)
    assert guard.events.count("sync_relays") == previous_syncs
    assert synced == []
    # A fresh call can reconcile the completed rollout; no failed snapshot is
    # retained by the manager.
    manager.require_network(spec)
    assert len(synced) == 1


@pytest.mark.parametrize("change", ["alias", "gateway_priority", "missing"])
def test_relay_endpoint_change_is_detected_by_the_final_discovery(monkeypatch, change):
    manager, docker, guard, spec, name, synced = _running_kata_group(monkeypatch)
    discoveries = 0
    previous_syncs = guard.events.count("sync_relays")

    def change_endpoint(argv, **kwargs):
        nonlocal discoveries
        if argv[1:] == ["network", "inspect", "agent-relays"]:
            discoveries += 1
            if discoveries == 2:
                networks = docker.containers["relay-gateway"]["networks"]
                if change == "alias":
                    networks[name]["Aliases"] = []
                    networks[name]["DNSNames"] = []
                elif change == "gateway_priority":
                    networks[name]["GwPriority"] = 0
                else:
                    networks.pop(name)
        return docker(argv, **kwargs)

    monkeypatch.setattr(manager, "_run", change_endpoint)
    with pytest.raises(SandboxError, match="relay endpoint changed"):
        manager.require_network(spec)
    assert guard.events.count("sync_relays") == previous_syncs
    assert synced == []


@pytest.mark.parametrize("race", ["relay_rollout", "bridge_replacement"])
def test_attach_discards_network_evidence_from_before_connection(monkeypatch, race):
    manager, docker, guard = _world(monkeypatch, runtime=KATA_RUNTIME)
    spec = _spec()
    sandbox = docker.add_sandbox(spec)
    name = manager.network_for(spec)
    synced: list[dict[str, str]] = []
    old_gateway_address = ""
    mutated = False

    def connect_then_change(argv, **kwargs):
        nonlocal mutated, old_gateway_address
        result = docker(argv, **kwargs)
        if not mutated and argv[1:] == ["network", "connect", name, "sandbox-container"]:
            mutated = True
            network = docker.network(name)
            old_gateway_address = network["endpoints"]["relay-gateway"]["ip"]
            # Another trusted lifecycle actor starts the just-connected
            # container before this caller's post-connect inspection.
            sandbox["running"] = True
            sandbox["status"] = "running"
            sandbox["networks"][name]["NetworkID"] = network["Id"]
            if race == "relay_rollout":
                relays = docker.network("agent-relays")
                relays["endpoints"].pop("relay-gateway")
                docker.containers["relay-gateway"]["running"] = False
                docker.add_container("relay-gateway-new", running=True)
                docker.connect(relays["Id"], "relay-gateway-new", ["agent-gateway"])
            else:
                docker.networks.pop(network["Id"])
                docker.add_network(
                    name,
                    subnet=network["IPAM"]["Config"][0]["Subnet"],
                    options=network["Options"],
                    labels=network["Labels"],
                )
        return result

    monkeypatch.setattr(manager, "_run", connect_then_change)
    monkeypatch.setattr(
        manager,
        "_kata_privileged_exec",
        lambda _spec, argv, **_kwargs: (
            synced.append(json.loads(argv[3])) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )
    if race == "bridge_replacement":
        with pytest.raises(SandboxError, match="did not end up on exactly its group bridge"):
            manager.require_network(spec)
        assert synced == []
    else:
        manager.require_network(spec)
        endpoints = docker.network(name)["endpoints"]
        assert "relay-gateway" not in endpoints
        assert synced == [
            {
                "agent-gateway": endpoints["relay-gateway-new"]["ip"],
                "agent-mcp": endpoints["relay-mcp"]["ip"],
            }
        ]
        assert synced[0]["agent-gateway"] != old_gateway_address
        assert (synced[0]["agent-gateway"], 8080) in guard.relays[
            manager._network().bridge_name(name)
        ]
    assert mutated


def test_guest_network_snapshot_still_checks_files_and_skips_unchanged_writes(
    monkeypatch, tmp_path
):
    manager, _docker, _guard, spec, _name, _synced = _running_kata_group(monkeypatch)
    hosts = tmp_path / "hosts"
    resolv = tmp_path / "resolv.conf"
    hosts.write_text("127.0.0.1 localhost\n")
    resolv.write_text("nameserver 127.0.0.11\n")
    hosts.chmod(0o644)
    resolv.chmod(0o644)
    paths = {"/etc/hosts": hosts, "/etc/resolv.conf": resolv}
    actual_open, actual_stat = os.open, os.fstat
    monkeypatch.setattr(os, "open", lambda path, flags: actual_open(paths.get(path, path), flags))
    monkeypatch.setattr(
        os, "fstat", lambda fd: SimpleNamespace(st_uid=0, st_mode=actual_stat(fd).st_mode)
    )

    def run_script(_spec, argv, **_kwargs):
        monkeypatch.setattr(sys, "argv", ["-c", *argv[3:]])
        try:
            exec(compile(argv[2], "<guest-network>", "exec"), {})
        except (RuntimeError, OSError) as exc:
            return subprocess.CompletedProcess(argv, 1, "", str(exc))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(manager, "_kata_privileged_exec", run_script)
    current = manager._require(spec)
    addresses = {"agent-gateway": "10.240.9.2", "agent-mcp": "10.240.9.3"}
    manager._sync_kata_guest_network(spec, current, addresses)
    unchanged_mtime = 1_000_000_000
    for path in (hosts, resolv):
        os.utime(path, ns=(unchanged_mtime, unchanged_mtime))
    manager._sync_kata_guest_network(spec, current, addresses)
    assert [path.stat().st_mtime_ns for path in (hosts, resolv)] == [unchanged_mtime] * 2

    # Supplying already resolved addresses never skips the guest-owned-file
    # refusal or symlink protection, even after an earlier successful sync.
    resolv.chmod(0o666)
    with pytest.raises(SandboxError, match="root-owned and protected"):
        manager._sync_kata_guest_network(spec, current, addresses)
    resolv.chmod(0o644)
    hosts.write_text("127.0.0.1 localhost\n# BEGIN FREEINFERENCE RELAYS\nunterminated\n")
    with pytest.raises(SandboxError, match="invalid managed relay block"):
        manager._sync_kata_guest_network(spec, current, addresses)
    hosts.unlink()
    hosts.symlink_to(resolv)
    with pytest.raises(SandboxError, match="network sync failed"):
        manager._sync_kata_guest_network(spec, current, addresses)


# ── releasing and pruning ─────────────────────────────────────────────


def test_release_tolerates_the_null_network_and_an_already_pruned_bridge(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec)
    current = manager._require(spec)
    null = ChatSandboxInspection(**{**current.__dict__, "networks": (CHAT_NULL_NETWORK,)})
    assert manager._release_sandbox_networks(spec, null).container_id == "sandbox-container"
    gone = ChatSandboxInspection(**{**current.__dict__, "networks": ("already-pruned",)})
    docker.containers["sandbox-container"]["networks"]["already-pruned"] = {"NetworkID": "x"}
    manager._release_sandbox_networks(spec, gone)
    assert "already-pruned" not in docker.containers["sandbox-container"]["networks"]


def test_a_live_network_this_chat_does_not_own_is_still_a_violation(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec)
    other = docker.add_network("operator-network", subnet="172.19.0.0/16")
    docker.connect(other["Id"], "sandbox-container", [])
    with pytest.raises(SandboxError, match="not its group bridge"):
        manager._release_sandbox_networks(spec, manager._require(spec))


def test_a_stale_orphan_bridge_and_its_exceptions_are_pruned(monkeypatch):
    manager, docker, guard = _world(monkeypatch)
    orphan = _spec(thread="athr_gone", sandbox="asbx_gone")
    name, _ = _group_bridge(manager, docker, orphan, age_s=3600)
    manager._attach_relays(name)
    bridge = manager._network().bridge_name(name)
    guard.relays[bridge] = {("10.240.9.3", 8080)}

    manager.prune_orphan_networks([])

    assert docker.network(name) is None
    assert guard.relays[bridge] == set()


def test_prune_keeps_a_young_bridge_and_the_live_bridge_of_a_retained_chat(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    young, _ = _group_bridge(manager, docker, _spec(thread="athr_new", sandbox="asbx_new"))
    live_spec = _spec()
    docker.add_sandbox(live_spec, running=True)
    live, live_network = _group_bridge(
        manager, docker, live_spec, age_s=3600, subnet="10.240.10.0/29"
    )
    docker.connect(live_network["Id"], "sandbox-container", [])

    manager.prune_orphan_networks([live_spec])

    assert docker.network(young) is not None
    assert docker.network(live) is not None


def test_prune_reports_but_never_touches_legacy_or_foreign_networks(monkeypatch, caplog):
    manager, docker, _ = _world(monkeypatch)
    legacy = docker.add_network(
        "freeinference-chat-net-legacy",
        subnet="10.240.200.0/29",
        internal=True,
        labels={CHAT_NETWORK_LABEL: "true", CHAT_NAMESPACE_LABEL: _NAMESPACE},
        age_s=3600,
    )
    foreign_spec = _spec(thread="athr_f", sandbox="asbx_f")
    foreign = docker.add_network(
        "freeinference-chat-net-foreign",
        subnet="10.240.201.0/29",
        labels={
            **_owned_labels(manager, foreign_spec),
            CHAT_NAMESPACE_LABEL: "sibling-stack",
        },
        age_s=3600,
    )

    with caplog.at_level(logging.WARNING):
        manager.prune_orphan_networks([])

    assert docker.network(legacy["Id"]) is not None
    assert docker.network(foreign["Id"]) is not None
    assert "chat_legacy_network_found" in caplog.text


def test_opportunistic_cleanup_never_detaches_a_sandbox_that_attached_meanwhile(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec, running=True)
    name, network = _group_bridge(manager, docker, spec, age_s=3600)
    docker.connect(network["Id"], "sandbox-container", [])

    manager._release_owned_network(
        name, spec, only_if_sandbox_unattached=True, expected_network_id=network["Id"]
    )

    assert "sandbox-container" in docker.network(name)["endpoints"]


def test_stale_cleanup_leaves_a_same_name_replacement_alone(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    name, _ = _group_bridge(manager, docker, spec, age_s=3600)

    manager._release_owned_network(
        name, spec, only_if_sandbox_unattached=True, expected_network_id="the-old-id"
    )

    assert docker.network(name) is not None


def test_final_network_release_refuses_a_same_name_container_replacement(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    original = docker.add_sandbox(spec, running=False)
    name, network = _group_bridge(manager, docker, spec)
    docker.connect(network["Id"], original["Id"], [])
    release = manager._release_sandbox_networks

    def replace_before_release(release_spec, current, **kwargs):
        original["Name"] = "/renamed-original"
        replacement = docker.add_container(
            "replacement-container",
            name=spec.container_name,
            running=False,
            labels={**spec.docker_labels(), CHAT_NAMESPACE_LABEL: _NAMESPACE},
            image=spec.sandbox_image_digest or "img",
        )
        docker.connect(network["Id"], replacement["Id"], [])
        return release(release_spec, current, **kwargs)

    monkeypatch.setattr(manager, "_release_sandbox_networks", replace_before_release)
    docker.calls.clear()

    with pytest.raises(RetainedChatLost, match="identity changed"):
        manager.release_stopped_networks(
            spec,
            expected_container_id=original["Id"],
        )

    assert docker.network(name) is not None
    assert docker.names_on(name) == {original["Id"], "replacement-container"}
    assert not any(
        call[1:3] in (["network", "disconnect"], ["network", "rm"]) for call in docker.calls
    )


def test_a_network_outside_this_chat_is_never_removed(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    name, network = _group_bridge(manager, docker, spec)
    network["Labels"][CHAT_NAMESPACE_LABEL] = "sibling-stack"
    with pytest.raises(SandboxError, match="outside this chat"):
        manager._release_owned_network(name, spec)
    assert docker.network(name) is not None


# ── ownership ─────────────────────────────────────────────────────────


def test_discovery_adopts_only_this_stacks_labelled_containers(monkeypatch):
    manager, _, _ = _world(monkeypatch)
    spec = _spec()

    def inspection(namespace: str | None) -> ChatSandboxInspection:
        labels = spec.docker_labels()
        if namespace is not None:
            labels[CHAT_NAMESPACE_LABEL] = namespace
        return ChatSandboxInspection(
            container_id="container",
            name=spec.container_name,
            status=ChatSandboxStatus.STOPPED,
            running=False,
            image=spec.sandbox_image_digest or "img",
            labels=labels,
            mounts=(),
            networks=("agent-relays",),
        )

    assert manager._belongs_to_namespace(inspection(_NAMESPACE))
    assert not manager._belongs_to_namespace(inspection("sibling-stack"))
    # Unlabelled containers predate the change and are left for the cleanup.
    assert not manager._belongs_to_namespace(inspection(None))


# ── cleanup racing an attach ──────────────────────────────────────────


def test_a_stale_unattached_bridge_is_reclaimed_and_rebuilt(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec)
    name, old = _group_bridge(manager, docker, spec, age_s=3600)

    manager._ensure_group_network(spec)

    rebuilt = docker.network(name)
    assert rebuilt is not None and rebuilt["Id"] != old["Id"]
    assert docker.names_on(name) == {"relay-gateway", "relay-mcp"}


def test_orphan_cleanup_restores_relays_when_a_sandbox_attaches_before_remove(monkeypatch):
    manager, docker, guard = _world(monkeypatch)
    spec = _spec(thread="athr_late", sandbox="asbx_late")
    name, network = _group_bridge(manager, docker, spec, age_s=3600)
    manager._attach_relays(name)
    docker.add_container("late-sandbox", running=True)
    docker.before_rm = lambda: docker.connect(network["Id"], "late-sandbox", [])

    manager.prune_orphan_networks([])

    assert docker.network(name) is not None, "a bridge in use survives"
    assert {"relay-gateway", "relay-mcp", "late-sandbox"} <= docker.names_on(name)
    assert guard.relays[manager._network().bridge_name(name)], "and keeps its exceptions"


def test_stale_cleanup_restores_relays_when_its_own_sandbox_attaches_before_remove(
    monkeypatch,
):
    manager, docker, guard = _world(monkeypatch)
    spec = _spec()
    docker.add_sandbox(spec, running=True)
    name, network = _group_bridge(manager, docker, spec, age_s=3600)
    manager._attach_relays(name)
    docker.before_rm = lambda: docker.connect(network["Id"], "sandbox-container", [])

    manager._release_owned_network(
        name, spec, only_if_sandbox_unattached=True, expected_network_id=network["Id"]
    )

    assert docker.names_on(name) == {"relay-gateway", "relay-mcp", "sandbox-container"}
    assert guard.relays[manager._network().bridge_name(name)]


# ── the guest's own check, executed against a model of its network ────


def _outcome(result: Any) -> None:
    if result == "timeout":
        raise TimeoutError("timed out")
    if isinstance(result, int):
        raise OSError(result, os.strerror(result))


class _GuestUdp:
    def __init__(self, outcomes: dict) -> None:
        self.outcomes = outcomes
        self.peer: tuple[str, int] | None = None

    def settimeout(self, _seconds: float) -> None:
        pass

    def connect(self, address: tuple[str, int]) -> None:
        self.peer = address

    def send(self, data: bytes) -> int:
        return len(data)

    def recv(self, _size: int) -> bytes:
        assert self.peer is not None
        result = self.outcomes[(*self.peer, "udp")]
        if isinstance(result, bytes):
            return result
        _outcome(result)
        return b"x"


class _GuestSockets:
    """Just the socket module surface the guest check touches."""

    AF_INET = socket.AF_INET
    SOCK_DGRAM = socket.SOCK_DGRAM
    inet_ntoa = staticmethod(socket.inet_ntoa)

    def __init__(self, outcomes: dict) -> None:
        self.outcomes = outcomes

    def socket(self, _family: int, _kind: int) -> _GuestUdp:
        return _GuestUdp(self.outcomes)

    def create_connection(self, address: tuple[str, int], timeout: float | None = None) -> Any:
        _outcome(self.outcomes[(*address, "tcp")])
        return SimpleNamespace(close=lambda: None)


_ROOT_ANSWER = bytes.fromhex("4321 8180 0001 000d 0000 0000".replace(" ", ""))


def _guest_check(monkeypatch, *, route: str = "0100F00A", **changes: Any) -> list[str]:
    """Run the check the way a guest does, with ``changes`` to what it meets."""
    outcomes: dict = {
        ("127.0.0.11", 53, "udp"): _ROOT_ANSWER,
        ("agent-gateway", 8080, "tcp"): "ok",
        ("agent-mcp", 8000, "tcp"): "ok",
        ("10.240.0.1", 9, "udp"): errno.EHOSTUNREACH,
        ("169.254.169.254", 80, "tcp"): errno.EHOSTUNREACH,
    }
    for key, value in changes.items():
        outcomes[
            {
                "resolver": ("127.0.0.11", 53, "udp"),
                "host": ("10.240.0.1", 9, "udp"),
                "metadata": ("169.254.169.254", 80, "tcp"),
                "relay": ("agent-mcp", 8000, "tcp"),
            }[key]
        ] = value
    files = {
        "/proc/net/route": "Iface\tDestination\tGateway\tFlags\n"
        + (f"eth0\t00000000\t{route}\t0003\n" if route else "")
        + "eth0\t0000F00A\t00000000\t0001\n",
        "/etc/resolv.conf": "nameserver 127.0.0.11\noptions ndots:0\n",
    }
    monkeypatch.setitem(sys.modules, "socket", _GuestSockets(outcomes))
    monkeypatch.setattr(
        sys,
        "argv",
        ["-c", "127.0.0.11", json.dumps([["agent-gateway", 8080], ["agent-mcp", 8000]])],
    )
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        exec(  # the shipped script, run as the guest runs it
            compile(_GUEST_NETWORK_CHECK, "<guest-network-check>", "exec"),
            {"__name__": "__main__", "open": lambda path: io.StringIO(files[path])},
        )
    return json.loads(printed.getvalue())


def test_a_guest_behind_its_rules_passes(monkeypatch):
    assert _guest_check(monkeypatch) == []


@pytest.mark.parametrize(
    ("change", "value", "problem"),
    [
        ("resolver", "timeout", "resolver 127.0.0.11 did not answer"),
        # SERVFAIL: the resolver is there, but cannot resolve for this guest.
        ("resolver", bytes.fromhex("4321 8182 0001 0000 0000 0000".replace(" ", "")), "usefully"),
        ("resolver", bytes.fromhex("9999 8180 0001 0000 0000 0000".replace(" ", "")), "usefully"),
        ("relay", errno.ECONNREFUSED, "relay agent-mcp:8000 unreachable"),
        # The host answered "port unreachable" itself: no rule in the path.
        ("host", errno.ECONNREFUSED, "refused by ECONNREFUSED, not by the network rules"),
        ("host", "timeout", "was not refused within 2s"),
        ("metadata", "ok", "answered; the network rules are not in this guest's path"),
        ("metadata", "timeout", "was not refused within 2s"),
    ],
)
def test_the_guest_check_names_what_it_found(monkeypatch, change, value, problem):
    problems = _guest_check(monkeypatch, **{change: value})
    assert len(problems) == 1 and problem in problems[0], problems


def test_a_guest_with_no_default_route_is_reported_without_probing_the_host(monkeypatch):
    problems = _guest_check(monkeypatch, route="")
    assert problems == ["no default route"]
