"""Per-group bridges, relay membership, and fail-closed sandbox network proof.

The container manager supplies exact identity checks and bounded Docker/guest
operations. This component owns network creation, reconciliation, verification,
and release; it never owns member state or archive policy. Fresh evidence is
kept only for the current operation, never cached across starts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent_host.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_NETWORK_GENERATION_LABEL,
    CHAT_NETWORK_LABEL,
    CHAT_NETWORK_POOL_LABEL,
    CHAT_NETWORK_SANDBOX_ID_LABEL,
    CHAT_NETWORK_SERVICES_LABEL,
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatFailStopped,
    RetainedChatLost,
)
from cloud_agent_host.network_guard import GUARD_RENEW_S
from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError

if TYPE_CHECKING:
    import builtins
    import subprocess
    from collections.abc import Iterator, Sequence

    from cloud_agent_host.sandbox import ContainerBackend
    from cloud_agent_shared.egress import SandboxNetwork
    from cloud_agent_shared.operation_profiling import StageObserver

# Keep the deployment's existing log selection and operational event names.
logger = logging.getLogger("cloud_agent_host.chat_sandbox")

CHAT_NETWORK_PREFIX = "freeinference-chat-net"
CHAT_NETWORK_PREFIX_LENGTH = 29
CHAT_NETWORK_PRUNE_GRACE_S = 300
#: Docker's option naming a bridge's host interface. Group bridges carry this
#: deployment's prefix there, which is how the host's rules recognise them.
CHAT_NETWORK_BRIDGE_OPTION = "com.docker.network.bridge.name"
#: The gateway priority a relay joins every group bridge with. A container on
#: several networks takes its default route from the highest priority, then by
#: network name, and ``freeinference-chat-net-*`` sorts before a stack's own
#: network: at Docker's default of 0 the first group bridge a relay joined would
#: become its way out, and its SSH upstream would meet the sandbox rules — which
#: drop it, for every group sharing that relay. Below zero, never.
RELAY_GATEWAY_PRIORITY = -1
CHAT_BOOTSTRAP_NETWORK = "bridge"
#: Docker's built-in null network. A container reports it once its last real
#: endpoint is gone, which is exactly the state a completed release leaves
#: behind, so it means "no network" rather than "a network nobody claims".
CHAT_NULL_NETWORK = "none"
#: The resolver for a container created with no group network at all (the
#: guest observer probe runs on Docker's null network): loopback, so nothing
#: resolves. Every group sandbox is given the deployment's one resolver.
CHAT_OFFLINE_DNS = "127.0.0.1"

# Run inside a guest as the sandbox user after every cold start, with $1 the
# resolver the guest should use and $2 the relays as JSON. Prints the problems
# it found as a JSON list; an empty list is a pass. Every probe has a short
# bound and nothing here leaves the host: the resolver answers one query for the
# root's name servers, and the boundary probes aim at the host itself and at the
# two platform addresses (cloud metadata, and Azure's WireServer on a public
# address), which the host rules must refuse at once with their own ICMP
# "administratively prohibited". A host without the rules would answer "port
# unreachable" for itself instead, and route the others onwards.

_GUEST_NETWORK_CHECK = """
import errno, json, socket, struct, sys
resolver, relays = sys.argv[1], json.loads(sys.argv[2])
problems = []
gateway = None
for row in [line.split() for line in open('/proc/net/route').read().splitlines()[1:]]:
    if len(row) > 2 and row[1] == '00000000':
        gateway = socket.inet_ntoa(struct.pack('<L', int(row[2], 16)))
        break
if gateway is None:
    problems.append('no default route')
servers = [line.split()[1] for line in open('/etc/resolv.conf')
           if line.startswith('nameserver') and len(line.split()) > 1]
if servers != [resolver]:
    problems.append('resolver is %r, expected %r' % (servers, [resolver]))
query = bytes.fromhex('4321 0100 0001 0000 0000 0000 00 0002 0001'.replace(' ', ''))
try:
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.settimeout(3)
    dns.connect((resolver, 53))
    dns.send(query)
    answer = dns.recv(512)
    if len(answer) < 12 or answer[:2] != query[:2] or not answer[2] & 0x80 or answer[3] & 0x0F:
        problems.append('resolver %s did not answer the query usefully' % resolver)
except OSError as exc:
    problems.append('resolver %s did not answer: %s' % (resolver, exc))
for host, port in relays:
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as exc:
        problems.append('relay %s:%d unreachable: %s' % (host, port, exc))
def refused_by_rules(label, attempt):
    try:
        attempt()
    except TimeoutError:
        problems.append('%s was not refused within 2s' % label)
    except OSError as exc:
        if exc.errno != errno.EHOSTUNREACH:
            name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            problems.append('%s was refused by %s, not by the network rules' % (label, name))
    else:
        problems.append("%s answered; the network rules are not in this guest's path" % label)
def host_udp():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(2)
    probe.connect((gateway, 9))
    probe.send(b'x')
    probe.recv(1)
if gateway is not None:
    refused_by_rules('the host at %s:9/udp' % gateway, host_udp)
for label, address in (
    ('the metadata address 169.254.169.254:80', '169.254.169.254'),
    ('the platform address 168.63.129.16:80', '168.63.129.16'),
):
    refused_by_rules(
        label, lambda address=address: socket.create_connection((address, 80), timeout=2).close()
    )
print(json.dumps(problems))
"""


@dataclass
class _GroupNetworkSnapshot:
    """One reconciliation's evidence; never retained across commands or starts."""

    network: dict[str, Any] | None = None
    owners: dict[str, list[str]] = field(default_factory=dict)
    addresses: dict[str, str] | None = None


class ChatNetworkContainers(Protocol):
    """Exact container operations used by the group-network lifecycle."""

    @property
    def backend(self) -> ContainerBackend:
        """Return the deployment's Docker and sandbox-network configuration."""
        ...

    def _run(
        self, argv: builtins.list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...

    def _require(self, spec: ChatSandboxSpec) -> ChatSandboxInspection: ...

    def _verify_identity(
        self, inspection: ChatSandboxInspection, spec: ChatSandboxSpec, *, require_name: bool = True
    ) -> None: ...

    def inspect(self, container_name: str) -> ChatSandboxInspection | None:
        """Inspect a container without replacing its immutable identity."""
        ...

    def _kata_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        detach: bool = False,
        timeout_s: float = 30,
    ) -> subprocess.CompletedProcess[str]: ...

    def exec_capture(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = "/workspace",
        timeout_s: float = 60,
        input_bytes: bytes | None = None,
        user_uid: int | None = None,
        user_gid: int | None = None,
        observer: StageObserver | None = None,
        expected_container_id: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a bounded guest command using the container manager's identity gate."""
        ...

    def stop(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        release_networks: bool = True,
    ) -> ChatSandboxInspection:
        """Exact-stop a guest whose network proof failed."""
        ...


class ChatSandboxNetwork:
    """Own one group's bridge and relay boundary through narrow container operations."""

    def __init__(self, containers: ChatNetworkContainers) -> None:
        self._containers = containers

    @property
    def backend(self) -> ContainerBackend:
        """Return the owning manager's deployment configuration."""
        return self._containers.backend

    def _network(self) -> SandboxNetwork:
        """This deployment's network; only group-network work needs it."""
        return self.backend.require_sandbox_network()

    def network_for(self, spec: ChatSandboxSpec) -> str:
        """Return this group's one network name.

        It depends on nothing but the group's identity, so setup, the agent
        and every fork member meet on the same bridge for the life of the
        sandbox generation, and a new generation gets a new bridge.
        """
        digest = hashlib.sha256(
            (
                f"{self.backend.sandbox_namespace}\0{spec.thread_id}\0{spec.sandbox_id}\0"
                f"{spec.generation}"
            ).encode()
        ).hexdigest()[:24]
        return f"{CHAT_NETWORK_PREFIX}-{digest}"

    def _relay_aliases(self) -> tuple[str, ...]:
        """The relay names a group bridge carries, validated by the config."""
        return self._network().relay_hosts

    def _network_labels(self, spec: ChatSandboxSpec) -> dict[str, str]:
        return {
            CHAT_NETWORK_LABEL: "true",
            CHAT_NAMESPACE_LABEL: self.backend.sandbox_namespace,
            CHAT_NETWORK_SANDBOX_ID_LABEL: spec.sandbox_id,
            CHAT_NETWORK_GENERATION_LABEL: str(spec.generation),
            CHAT_NETWORK_SERVICES_LABEL: ",".join(self._relay_aliases()),
            CHAT_NETWORK_POOL_LABEL: str(self._network().pool),
        }

    def _inspect_network(self, network: str) -> dict[str, Any] | None:
        result = self._containers._run(
            [self.backend.docker_binary, "network", "inspect", network], timeout=30
        )
        if result.returncode != 0:
            lowered = result.stderr.lower()
            if "no such network" in lowered or "not found" in lowered:
                return None
            raise SandboxError("Docker network inspect failed: " + result.stderr.strip()[:300])
        try:
            payload = json.loads(result.stdout)
            raw = payload[0]
        except (IndexError, TypeError, json.JSONDecodeError) as exc:
            raise SandboxError("Docker returned an invalid network inspection") from exc
        if not isinstance(raw, dict):
            raise SandboxError("Docker returned an invalid network inspection")
        return raw

    def _verify_group_network(self, network: dict[str, Any], spec: ChatSandboxSpec) -> None:
        expected_name = self.network_for(spec)
        expected_labels = self._network_labels(spec)
        labels = network.get("Labels") or {}
        options = network.get("Options") or {}
        mismatched = {
            key: (labels.get(key), value)
            for key, value in expected_labels.items()
            if labels.get(key) != value
        }
        if (
            network.get("Name") != expected_name
            or network.get("Driver") != "bridge"
            or network.get("Scope") not in (None, "local")
            # Routable on purpose: the host's rules, not Docker's internal flag,
            # decide what a group reaches. An internal network carrying this
            # name is the old per-phase topology, which is never adopted.
            or network.get("Internal") is not False
            or network.get("EnableIPv6") is True
            or not isinstance(options, dict)
            or options.get(CHAT_NETWORK_BRIDGE_OPTION) != self._network().bridge_name(expected_name)
            or not self._network_uses_chat_pool(network)
            or mismatched
        ):
            raise SandboxError(
                "Docker network name is occupied outside this chat/generation or is not "
                "this deployment's guarded group bridge"
            )

    def _network_uses_chat_pool(self, network: dict[str, Any]) -> bool:
        """Whether Docker assigned exactly one /29 from the configured pool."""
        configs = (network.get("IPAM") or {}).get("Config") or []
        if not isinstance(configs, list):
            return False
        ipv4_subnets: builtins.list[ipaddress.IPv4Network] = []
        for config in configs:
            if not isinstance(config, dict) or not config.get("Subnet"):
                continue
            try:
                subnet = ipaddress.ip_network(str(config["Subnet"]), strict=True)
            except ValueError:
                return False
            if isinstance(subnet, ipaddress.IPv4Network):
                ipv4_subnets.append(subnet)
        return bool(
            len(ipv4_subnets) == 1
            and ipv4_subnets[0].prefixlen == CHAT_NETWORK_PREFIX_LENGTH
            and ipv4_subnets[0].subnet_of(self._network().pool)
        )

    def _inspect_containers(self, container_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Inspect many containers in one call, keyed by the id asked for.

        ``docker container inspect`` takes any number of ids and answers for
        all of them at once, and the marginal cost of an extra id is a few
        hundred microseconds against roughly twelve milliseconds for the
        process itself. Asking once per container — which is what the callers
        below used to do — made a reconcile's cost scale with the size of the
        relay network for no reason.

        An id that does not resolve is simply absent from the result. Docker
        exits non-zero when *any* id is missing while still answering for the
        rest, so the output is parsed regardless of the exit code: a partial
        answer is the normal case here, since Compose replacement can remove
        an endpoint between the network inspection and this one.
        """
        wanted = list(dict.fromkeys(str(container_id) for container_id in container_ids))
        if not wanted:
            return {}
        result = self._containers._run(
            [self.backend.docker_binary, "container", "inspect", *wanted], timeout=30
        )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, list):
            return {}

        found: dict[str, dict[str, Any]] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            identifier = str(raw.get("Id") or "")
            name = str(raw.get("Name") or "").removeprefix("/")
            for container_id in wanted:
                if container_id in found:
                    continue
                # A caller may hold a full id, a short id, or a name; Docker
                # always answers with the full id and the name.
                if container_id in (identifier, name) or identifier.startswith(container_id):
                    found[container_id] = raw
                    break
        return found

    @staticmethod
    def _endpoint_aliases(raw: dict[str, Any], network: str) -> set[str] | None:
        """Every name one container answers to on ``network``, or ``None``."""
        endpoint = ((raw.get("NetworkSettings") or {}).get("Networks") or {}).get(network)
        if endpoint is None:
            return None
        return {
            str(alias).lower().rstrip(".")
            for alias in [*(endpoint.get("Aliases") or []), *(endpoint.get("DNSNames") or [])]
            if alias
        }

    def _network_aliases_for(
        self, container_ids: Sequence[str], network: str
    ) -> dict[str, set[str]]:
        """``_container_network_aliases`` for many containers, in one call."""
        inspected = self._inspect_containers(container_ids)
        resolved: dict[str, set[str]] = {}
        for container_id, raw in inspected.items():
            aliases = self._endpoint_aliases(raw, network)
            if aliases is not None:
                resolved[container_id] = aliases
        return resolved

    def _discover_relays(self) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
        """Resolve owners and keep their inspected endpoints for this operation."""
        base = self._network().relay_network
        aliases = self._relay_aliases()
        network = self._inspect_network(base)
        if network is None:
            raise SandboxError(f"configured relay network {base!r} does not exist")
        containers = network.get("Containers") or {}
        if not isinstance(containers, dict):
            raise SandboxError(f"configured relay network {base!r} is invalid")
        matches: dict[str, list[str]] = {alias: [] for alias in aliases}
        # One inspection for the whole relay network. A container that Compose
        # replaced between the network inspection and this one is simply
        # absent, and the exact-one check below turns that race into a
        # retryable activation failure rather than selecting a different
        # container — which is what the per-container non-zero exit used to do.
        inspected = self._inspect_containers([str(entry) for entry in containers])
        for container_id, raw in inspected.items():
            try:
                if not bool((raw.get("State") or {}).get("Running")):
                    continue
                endpoint = ((raw.get("NetworkSettings") or {}).get("Networks") or {}).get(
                    base
                ) or {}
                names = {
                    str(name).lower().rstrip(".")
                    for name in [
                        str(raw.get("Name") or "").removeprefix("/"),
                        *((raw.get("Config") or {}).get("Hostname") or "",),
                        *(endpoint.get("Aliases") or []),
                        *(endpoint.get("DNSNames") or []),
                    ]
                    if name
                }
            except (TypeError, AttributeError):
                continue
            for alias in aliases:
                if alias in names:
                    matches[alias].append(container_id)

        owners: dict[str, list[str]] = {}
        for alias, candidates in matches.items():
            unique = list(dict.fromkeys(candidates))
            if len(unique) != 1:
                detail = "no running container" if not unique else "multiple running containers"
                raise SandboxError(
                    f"relay alias {alias!r} has {detail} on relay network "
                    f"{base!r}; refusing an ambiguous group network"
                )
            owners.setdefault(unique[0], []).append(alias)
        return owners, inspected

    def _relay_owners(self) -> dict[str, list[str]]:
        """Resolve each relay alias to one running container on the relay network."""
        return self._discover_relays()[0]

    def _container_network_aliases(self, container_id: str, network: str) -> set[str] | None:
        """One container's names on ``network``; ``None`` if it is not on it."""
        return self._network_aliases_for([container_id], network).get(container_id)

    def relay_addresses(self, network: str) -> dict[str, str]:
        """Resolve only the configured relays to their addresses on one bridge."""
        if not self._relay_aliases():
            return {}
        owners = self._relay_owners()
        raw = self._inspect_network(network)
        return self._relay_addresses_from(owners, raw)

    @staticmethod
    def _relay_addresses_from(
        owners: dict[str, list[str]], raw: dict[str, Any] | None
    ) -> dict[str, str]:
        """Read relay addresses from the same bridge whose membership was checked."""
        endpoints = (raw or {}).get("Containers") or {}
        if not isinstance(endpoints, dict):
            raise SandboxError("Docker returned invalid group-network endpoints")
        addresses: dict[str, str] = {}
        for container_id, names in owners.items():
            endpoint = endpoints.get(container_id) or {}
            try:
                address = ipaddress.IPv4Interface(endpoint["IPv4Address"]).ip
            except (KeyError, ValueError, TypeError) as exc:
                raise SandboxError("relay has no IPv4 address on the group network") from exc
            for alias in names:
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", alias):
                    raise SandboxError("relay alias is not a hostname")
                addresses[alias] = str(address)
        return addresses

    def _sync_kata_guest_network(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        addresses: dict[str, str] | None = None,
    ) -> None:
        """Write the guest's relay names and its one resolver after activation.

        Docker's 127.0.0.11 resolver lives in a host network namespace, not in
        the Kata guest, so neither the relay aliases nor any other name
        resolves there by itself. A trusted guest-root helper writes both: the
        relay block in ``/etc/hosts`` and this deployment's resolver in
        ``/etc/resolv.conf``, the only DNS server the host's rules let a group
        reach. Under runc the container shares Docker's resolver, which
        forwards to the same server, and needs neither.
        """
        if self.backend.runtime != KATA_RUNTIME or not current.running:
            return
        network = self.network_for(spec)
        if current.networks != (network,):
            return
        if addresses is None:
            addresses = self.relay_addresses(network)
        script = (
            "import fcntl, ipaddress, json, os, stat, sys\n"
            "entries=json.loads(sys.argv[1])\n"
            "resolver=str(ipaddress.IPv4Address(sys.argv[2]))\n"
            "def protected(f, name):\n"
            " s=os.fstat(f.fileno())\n"
            " if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:\n"
            "  raise RuntimeError('guest '+name+' is not root-owned and protected')\n"
            "begin='# BEGIN FREEINFERENCE RELAYS\\n'\n"
            "end='# END FREEINFERENCE RELAYS\\n'\n"
            "fd=os.open('/etc/hosts', os.O_RDWR | os.O_NOFOLLOW)\n"
            "with os.fdopen(fd, 'r+') as f:\n"
            " fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            " protected(f, 'hosts file')\n"
            " original=f.read(65537)\n"
            " text=original\n"
            " if len(text)>65536: raise RuntimeError('guest hosts file is too large')\n"
            " if begin in text:\n"
            "  before, rest=text.split(begin,1)\n"
            "  if end not in rest: raise RuntimeError('invalid managed relay block')\n"
            "  text=before+rest.split(end,1)[1]\n"
            " text=text.rstrip()+'\\n'+begin\n"
            " text+=''.join(ip+' '+host+'\\n' for host,ip in sorted(entries.items()))\n"
            " if original != text+end:\n"
            "  f.seek(0); f.write(text+end); f.truncate()\n"
            "fd=os.open('/etc/resolv.conf', os.O_RDWR | os.O_NOFOLLOW)\n"
            "with os.fdopen(fd, 'r+') as f:\n"
            " fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            " protected(f, 'resolv.conf')\n"
            " desired='nameserver '+resolver+'\\n'\n"
            " if f.read(len(desired)+1) != desired:\n"
            "  f.seek(0); f.write(desired); f.truncate()\n"
        )
        result = self._containers._kata_privileged_exec(
            spec,
            ["python3", "-c", script, json.dumps(addresses), str(self._network().dns_server)],
            timeout_s=30,
        )
        if result.returncode != 0:
            raise SandboxError("Kata guest network sync failed: " + result.stderr[:300])

    def _relay_endpoint(self, container_id: str, network: str) -> tuple[set[str], int] | None:
        """A relay's names and gateway priority on ``network``; ``None`` if absent."""
        raw = self._inspect_containers([container_id]).get(container_id)
        return self._relay_endpoint_from(raw, network)

    @classmethod
    def _relay_endpoint_from(
        cls, raw: dict[str, Any] | None, network: str
    ) -> tuple[set[str], int] | None:
        names = None if raw is None else cls._endpoint_aliases(raw, network)
        if raw is None or names is None:
            return None
        endpoint = raw["NetworkSettings"]["Networks"][network]
        try:
            priority = int(endpoint.get("GwPriority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return names, priority

    def _ensure_service_connection(
        self,
        *,
        network: str,
        container_id: str,
        aliases: builtins.list[str],
        inspected: dict[str, Any] | None = None,
    ) -> None:
        existing = (
            self._relay_endpoint_from(inspected, network)
            if inspected is not None
            else self._relay_endpoint(container_id, network)
        )
        if existing is not None:
            names, priority = existing
            if not set(aliases).issubset(names):
                raise SandboxError(
                    f"fixed relay {container_id[:12]} is already attached to {network!r} "
                    "without the required aliases"
                )
            if priority < 0:
                return
            # Attached without the priority, by hand or by an earlier build: this
            # bridge may already be the relay's way out. Attach it again below.
            self._disconnect_endpoint(network, container_id)
        command = [
            self.backend.docker_binary,
            "network",
            "connect",
            "--gw-priority",
            str(RELAY_GATEWAY_PRIORITY),
        ]
        for alias in sorted(aliases):
            command += ["--alias", alias]
        command += [network, container_id]
        connected = self._containers._run(command, timeout=30)
        observed = self._relay_endpoint(container_id, network)
        if connected.returncode != 0 and observed is None:
            raise SandboxError(
                "fixed relay could not join the chat network: " + connected.stderr.strip()[:300]
            )
        if observed is None or not set(aliases).issubset(observed[0]):
            raise SandboxError("fixed relay joined the chat network without its required aliases")
        if observed[1] >= 0:
            raise SandboxError(
                "fixed relay joined the chat network without its gateway priority, so the "
                "group bridge could become the relay's default route; this Docker does not "
                "honour --gw-priority (Docker 28 or newer is required)"
            )

    def _reconcile_group_members(
        self,
        *,
        network: str,
        spec: ChatSandboxSpec,
        service_ids: set[str],
        service_aliases: tuple[str, ...],
        sandbox: ChatSandboxInspection | None = None,
    ) -> dict[str, Any]:
        """Keep a group bridge limited to this chat and its current relays.

        Compose replaces relay containers during a rollout. Their old endpoints
        can remain on a retained group bridge after the relay network names the
        replacement. An old endpoint carrying one of the reviewed relay aliases
        is safe to detach; any other peer is an ownership violation and fails
        closed rather than being silently evicted.
        """
        inspected = self._inspect_network(network)
        if inspected is None:
            raise SandboxError("group network disappeared during reconciliation")
        self._verify_group_network(inspected, spec)
        containers = inspected.get("Containers") or {}
        if not isinstance(containers, dict):
            raise SandboxError("Docker returned invalid group-network endpoints")

        permitted = set(service_ids)
        if sandbox is None:
            sandbox = self._containers.inspect(spec.container_name)
        if sandbox is not None:
            self._containers._verify_identity(sandbox, spec)
            permitted.add(sandbox.container_id)

        required_aliases = set(service_aliases)
        unexpected = sorted(set(map(str, containers)) - permitted)
        peer_aliases = self._network_aliases_for(unexpected, network)
        for container_id in unexpected:
            endpoint_aliases = peer_aliases.get(container_id)
            if not endpoint_aliases or not endpoint_aliases.intersection(required_aliases):
                raise SandboxError(f"group network {network!r} contains an unexpected peer")
            disconnected = self._containers._run(
                [
                    self.backend.docker_binary,
                    "network",
                    "disconnect",
                    network,
                    container_id,
                ],
                timeout=30,
            )
            if disconnected.returncode != 0:
                refreshed = self._inspect_network(network)
                remaining = (refreshed or {}).get("Containers") or {}
                if container_id in remaining:
                    raise SandboxError(
                        "stale relay could not leave the group network: "
                        + disconnected.stderr.strip()[:300]
                    )

        refreshed = self._inspect_network(network)
        if refreshed is None:
            raise SandboxError("group network disappeared during reconciliation")
        self._verify_group_network(refreshed, spec)
        if refreshed.get("Id") != inspected.get("Id"):
            raise SandboxError("group network identity changed during reconciliation")
        refreshed_containers = refreshed.get("Containers") or {}
        if not isinstance(refreshed_containers, dict):
            raise SandboxError("Docker returned invalid group-network endpoints")
        actual = set(map(str, refreshed_containers))
        if not service_ids.issubset(actual) or actual - permitted:
            raise SandboxError("group network membership is not exact")
        return refreshed

    def _candidate_chat_subnets(self, network_name: str) -> Iterator[ipaddress.IPv4Network]:
        """Yield every /29 once, starting from a stable per-network hash.

        Docker remains the cross-process allocator: two runner replicas may
        race on one subnet, but only one ``network create`` can claim it. The
        loser advances through this deterministic ring instead of relying on a
        process-local lock that would not cover the sandbox manager and runner replicas.
        """
        pool = self._network().pool
        subnet_count = 1 << (CHAT_NETWORK_PREFIX_LENGTH - pool.prefixlen)
        start = int.from_bytes(hashlib.sha256(network_name.encode()).digest()[:8], "big")
        start %= subnet_count
        pool_start = int(pool.network_address)
        subnet_size = 1 << (32 - CHAT_NETWORK_PREFIX_LENGTH)
        for offset in range(subnet_count):
            index = (start + offset) % subnet_count
            yield ipaddress.IPv4Network(
                (pool_start + index * subnet_size, CHAT_NETWORK_PREFIX_LENGTH)
            )

    @staticmethod
    def _is_subnet_allocation_conflict(stderr: str) -> bool:
        lowered = stderr.lower()
        return any(
            marker in lowered
            for marker in (
                "pool overlaps",
                "overlaps with other one",
                "requested subnet is already allocated",
                "address space is already allocated",
            )
        )

    def _create_group_network(self, expected: str, spec: ChatSandboxSpec) -> dict[str, Any]:
        last_conflict = ""
        bridge = self._network().bridge_name(expected)
        for subnet in self._candidate_chat_subnets(expected):
            command = [
                self.backend.docker_binary,
                "network",
                "create",
                "--driver",
                "bridge",
                # The interface name is how the host's rules recognise this
                # deployment's groups, so it is chosen here, never by Docker.
                "--opt",
                f"{CHAT_NETWORK_BRIDGE_OPTION}={bridge}",
                "--subnet",
                str(subnet),
            ]
            for key, value in sorted(self._network_labels(spec).items()):
                command += ["--label", f"{key}={value}"]
            command.append(expected)
            created = self._containers._run(command, timeout=30)
            network = self._inspect_network(expected)
            if network is not None:
                # This includes a same-name create won by another process. The
                # caller verifies every ownership label and the chosen subnet.
                return network
            if created.returncode == 0:
                raise SandboxError("Docker created the group network but it cannot be inspected")
            if not self._is_subnet_allocation_conflict(created.stderr):
                raise SandboxError(
                    "group network could not be created: " + created.stderr.strip()[:300]
                )
            last_conflict = created.stderr.strip()[:300]
        raise SandboxError(
            f"AGENT_EGRESS_CHAT_NETWORK_POOL {self._network().pool} has no free "
            f"/{CHAT_NETWORK_PREFIX_LENGTH} subnet"
            + (f": {last_conflict}" if last_conflict else "")
        )

    def _attach_relays(
        self,
        network: str,
        discovery: tuple[dict[str, list[str]], dict[str, dict[str, Any]]] | None = None,
    ) -> set[str]:
        """Attach the exact currently discovered relay endpoints to one bridge."""
        owners, inspected = discovery if discovery is not None else self._discover_relays()
        for container_id, owned_aliases in owners.items():
            self._ensure_service_connection(
                network=network,
                container_id=container_id,
                aliases=owned_aliases,
                inspected=inspected.get(container_id),
            )
        return set(owners)

    def _sync_relay_exceptions(
        self,
        network: str,
        addresses: dict[str, str] | None = None,
        *,
        network_id: str | None = None,
    ) -> None:
        """Make the host's relay exceptions for one bridge match its relays.

        One exception per configured endpoint, not per relay name: a single
        relay may serve both the gateway and the MCP port. ``network_id``, the
        bridge's Docker id from this reconciliation, lets the guard skip an
        update it already made for that same network.
        """
        config = self._network()
        if addresses is None:
            addresses = self.relay_addresses(network)
        self.backend.network_guard().sync_relays(
            config.bridge_name(network),
            {
                (addresses[relay.host], relay.port)
                for relay in config.relays
                if relay.host in addresses
            },
            network_id=network_id,
        )

    def _drop_relay_exceptions(self, network_name: str) -> None:
        """Forget a removed bridge's relay and SSH exceptions, without failing the removal.

        They match nothing once the bridge is gone, and a later bridge with the
        same name has its own relays written when they attach and its own SSH
        targets written before its first turn. A guard that cannot run here
        fails the next activation instead.
        """
        try:
            self.backend.network_guard().release_bridge(self._network().bridge_name(network_name))
        except SandboxError as exc:
            logger.warning(
                "chat_group_relay_exceptions_left",
                extra={"network": network_name, "detail": str(exc)[:300]},
            )

    def _ensure_group_services(
        self,
        network: str,
        spec: ChatSandboxSpec,
        *,
        sandbox: ChatSandboxInspection | None = None,
        sync_exceptions: bool = True,
    ) -> _GroupNetworkSnapshot:
        """Attach and verify the relays, then mirror them into the host's rules.

        ``sync_exceptions=False`` leaves the host's relay exceptions as they are,
        for a caller that reconciles again before anything can send on the
        bridge and syncs them then.
        """
        aliases = self._relay_aliases()
        discovery = self._discover_relays() if aliases else ({}, {})
        owners = discovery[0]
        service_ids = self._attach_relays(network, discovery) if aliases else set()
        reconciled = self._reconcile_group_members(
            network=network,
            spec=spec,
            service_ids=service_ids,
            service_aliases=aliases,
            sandbox=sandbox,
        )
        # Compose can replace a relay while reconciliation runs. Refuse that
        # race rather than giving an old owner's address to the guest or nft.
        if aliases:
            latest_owners, latest_inspected = self._discover_relays()
            if latest_owners != owners:
                raise SandboxError("relay owners changed during group network reconciliation")
            for container_id, owned_aliases in owners.items():
                endpoint = self._relay_endpoint_from(latest_inspected.get(container_id), network)
                if (
                    endpoint is None
                    or not set(owned_aliases).issubset(endpoint[0])
                    or endpoint[1] >= 0
                ):
                    raise SandboxError("relay endpoint changed during group network reconciliation")
        addresses = self._relay_addresses_from(owners, reconciled)
        if sync_exceptions:
            self._sync_relay_exceptions(
                network, addresses, network_id=str(reconciled.get("Id") or "") or None
            )
        return _GroupNetworkSnapshot(network=reconciled, owners=owners, addresses=addresses)

    def sync_ssh_targets(self, spec: ChatSandboxSpec, endpoints: set[tuple[str, int]]) -> None:
        """Make the group bridge's SSH exceptions exactly ``endpoints``.

        The caller proves the group network first (:meth:`require_network`
        through the manager), so the bridge exists and the rules are verified.
        """
        self.backend.network_guard().sync_ssh_targets(
            self._network().bridge_name(self.network_for(spec)), endpoints
        )

    def require_host_rules(self) -> None:
        """Install and verify this deployment's host rules, unless a recent check stands."""
        self.backend.network_guard().ensure()

    def renew_host_rules(self) -> None:
        """Renew the host-rule verification before it lapses, for the sweep.

        A host with no sandbox network configured has nothing to keep warm.
        """
        if self.backend.sandbox_network is None:
            return
        self.backend.network_guard().ensure(max_age_s=GUARD_RENEW_S)

    def _ensure_group_network(
        self,
        spec: ChatSandboxSpec,
        *,
        current: ChatSandboxInspection | None = None,
        snapshot: _GroupNetworkSnapshot | None = None,
        sync_exceptions: bool = True,
    ) -> str:
        """Create/reconcile the group bridge, its relays and their exceptions."""
        # Rules first: no group bridge exists on a host that has not proved it
        # enforces them.
        self.backend.network_guard().ensure()
        expected = self.network_for(spec)
        network = self._inspect_network(expected)
        if network is not None and self._network_is_stale_for_prune(network):
            sandbox = (
                current if current is not None else self._containers.inspect(spec.container_name)
            )
            if sandbox is None or not self._endpoint_is_current(sandbox, expected, network):
                network_id = str(network.get("Id") or "") or None
                self._release_owned_network(
                    expected,
                    spec,
                    only_if_sandbox_unattached=True,
                    expected_network_id=network_id,
                )
                network = self._inspect_network(expected)
        if network is None:
            network = self._create_group_network(expected, spec)
        if network is None:
            raise SandboxError("Docker created the group network but it cannot be inspected")
        self._verify_group_network(network, spec)
        result = self._ensure_group_services(
            expected, spec, sandbox=current, sync_exceptions=sync_exceptions
        )
        if snapshot is not None:
            snapshot.network = result.network
            snapshot.owners = result.owners
            snapshot.addresses = result.addresses
        return expected

    def _owned_network_matches(self, network_name: str, spec: ChatSandboxSpec) -> bool:
        network = self._inspect_network(network_name)
        if network is None or network.get("Internal") is not False:
            return False
        labels = network.get("Labels") or {}
        return bool(
            labels.get(CHAT_NETWORK_LABEL) == "true"
            and labels.get(CHAT_NAMESPACE_LABEL) == self.backend.sandbox_namespace
            and labels.get(CHAT_NETWORK_SANDBOX_ID_LABEL) == spec.sandbox_id
            and labels.get(CHAT_NETWORK_GENERATION_LABEL) == str(spec.generation)
        )

    def _disconnect_endpoint(
        self,
        network: str,
        container_id: str,
        *,
        network_name: str | None = None,
    ) -> None:
        disconnected = self._containers._run(
            [
                self.backend.docker_binary,
                "network",
                "disconnect",
                "--force",
                network,
                container_id,
            ],
            timeout=30,
        )
        if disconnected.returncode == 0:
            return
        if self._inspect_network(network) is None:
            # An id-pinned cleanup can lose to another process that already
            # removed the old bridge. A same-name replacement is unrelated.
            return
        aliases = self._container_network_aliases(container_id, network_name or network)
        if aliases is not None:
            raise SandboxError(
                f"container {container_id[:12]} could not leave network "
                f"{(network_name or network)!r}: " + disconnected.stderr.strip()[:300]
            )

    def _release_owned_network(
        self,
        network_name: str,
        spec: ChatSandboxSpec,
        *,
        only_if_sandbox_unattached: bool = False,
        expected_network_id: str | None = None,
        expected_container_id: str | None = None,
    ) -> None:
        """Disconnect exact owned endpoints and delete one inactive bridge."""
        network = self._inspect_network(network_name)
        if network is None:
            return
        network_id = str(network.get("Id") or "")
        if expected_network_id is not None and network_id != expected_network_id:
            # The old bridge disappeared and Docker reused its deterministic
            # name. Never let stale cleanup mutate the replacement.
            return
        network_ref = network_id or network_name
        labels = network.get("Labels") or {}
        if (
            network.get("Driver") != "bridge"
            or network.get("Internal") is not False
            or labels.get(CHAT_NETWORK_LABEL) != "true"
            or labels.get(CHAT_NAMESPACE_LABEL) != self.backend.sandbox_namespace
            or labels.get(CHAT_NETWORK_SANDBOX_ID_LABEL) != spec.sandbox_id
            or labels.get(CHAT_NETWORK_GENERATION_LABEL) != str(spec.generation)
        ):
            raise SandboxError("refusing to remove a network outside this chat/generation")
        containers = network.get("Containers") or {}
        if not isinstance(containers, dict):
            raise SandboxError("Docker returned invalid group-network endpoints")
        sandbox = self._containers.inspect(spec.container_name)
        if sandbox is not None:
            self._containers._verify_identity(sandbox, spec)
        if expected_container_id is not None and (
            sandbox is None or sandbox.container_id != expected_container_id
        ):
            raise RetainedChatLost("chat sandbox container identity changed during network release")
        sandbox_id = sandbox.container_id if sandbox is not None else None
        if only_if_sandbox_unattached and sandbox_id in containers:
            # Opportunistic orphan pruning must never detach a sandbox that
            # won the race and connected after the caller's first inspection.
            return
        recorded_aliases = {
            value.strip().lower().rstrip(".")
            for value in str(labels.get(CHAT_NETWORK_SERVICES_LABEL) or "").split(",")
            if value.strip()
        }
        peers = [
            container_id
            for container_id in sorted(map(str, containers))
            if container_id != sandbox_id
        ]
        peer_aliases = self._network_aliases_for(peers, network_name)
        for container_id in peers:
            endpoint_aliases = peer_aliases.get(container_id)
            if not endpoint_aliases or not endpoint_aliases.intersection(recorded_aliases):
                raise SandboxError(f"group network {network_name!r} contains an unexpected peer")
        for container_id in sorted(map(str, containers)):
            self._disconnect_endpoint(
                network_ref,
                container_id,
                network_name=network_name,
            )
        removed = self._containers._run(
            [self.backend.docker_binary, "network", "rm", network_ref],
            timeout=30,
        )
        remaining = self._inspect_network(network_ref) if removed.returncode != 0 else None
        if remaining is not None and only_if_sandbox_unattached:
            # A runner may attach the sandbox after the endpoint snapshot above.
            # Restore relays before deciding whether the failed opportunistic
            # prune is now a live group bridge.
            self._verify_group_network(remaining, spec)
            self._ensure_group_services(network_name, spec)
            refreshed_sandbox = self._containers.inspect(spec.container_name)
            if refreshed_sandbox is not None and self._endpoint_is_current(
                refreshed_sandbox, network_name
            ):
                return
        if remaining is not None:
            raise SandboxError(
                "inactive group network could not be removed: " + removed.stderr.strip()[:300]
            )
        self._drop_relay_exceptions(network_name)

    def _release_orphan_network(self, network: dict[str, Any]) -> None:
        """Delete one ownerless bridge by immutable ID and repair a lost attach race."""
        network_name = str(network.get("Name") or "")
        network_id = str(network.get("Id") or "")
        labels = network.get("Labels") or {}
        containers = network.get("Containers") or {}
        recorded_aliases = tuple(
            sorted(
                value.strip().lower().rstrip(".")
                for value in str(labels.get(CHAT_NETWORK_SERVICES_LABEL) or "").split(",")
                if value.strip()
            )
        )
        if not network_name or not network_id or not recorded_aliases:
            raise SandboxError("orphan group network has invalid ownership metadata")
        if not isinstance(containers, dict):
            raise SandboxError("orphan group network has invalid ownership metadata")
        peers = sorted(map(str, containers))
        peer_aliases = self._network_aliases_for(peers, network_name)
        for container_id in peers:
            endpoint_aliases = peer_aliases.get(container_id)
            if not endpoint_aliases or not endpoint_aliases.intersection(recorded_aliases):
                raise SandboxError("orphan group network contains an unexpected peer")
        for container_id in sorted(map(str, containers)):
            self._disconnect_endpoint(
                network_id,
                container_id,
                network_name=network_name,
            )
        removed = self._containers._run(
            [self.backend.docker_binary, "network", "rm", network_id],
            timeout=30,
        )
        remaining = self._inspect_network(network_id) if removed.returncode != 0 else None
        if remaining is None:
            self._drop_relay_exceptions(network_name)
            return

        # A new sandbox can attach to the old ID after the endpoint snapshot.
        # Put the relays back, with their host exceptions, before preserving
        # that now-live bridge.
        service_ids = self._attach_relays(network_name)
        self._sync_relay_exceptions(network_name)
        refreshed = self._inspect_network(network_id)
        refreshed_containers = (refreshed or {}).get("Containers") or {}
        if (
            isinstance(refreshed_containers, dict)
            and set(map(str, refreshed_containers)) - service_ids
        ):
            return
        raise SandboxError(
            "orphan group network could not be removed: " + removed.stderr.strip()[:300]
        )

    def _release_sandbox_networks(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        *,
        expected_container_id: str | None = None,
    ) -> ChatSandboxInspection:
        """Release the group bridge after the retained container stops."""
        if current.running:
            raise SandboxError("refusing to release networking from a running chat sandbox")
        if expected_container_id is not None:
            named = self._containers._require(spec)
            if named.container_id != expected_container_id:
                raise RetainedChatLost(
                    "chat sandbox container identity changed during network release"
                )
        for network_name in current.networks:
            if network_name == CHAT_NULL_NETWORK:
                # There is no connectivity here to withdraw. Docker reports the
                # null network for a container whose last endpoint has gone, so
                # this is what a release that already succeeded looks like --
                # and reading it as a network nobody owns made every
                # later stop of an already-released sandbox raise. Because the
                # supervisor stops every retained sandbox while reconciling, one
                # such container was enough to keep the sandbox manager from starting.
                continue
            if (
                network_name == CHAT_BOOTSTRAP_NETWORK
                and current.status is ChatSandboxStatus.CREATED
            ):
                # Recover an interrupted first create before the container ever
                # ran. The daemon-wide bridge itself is not owned by this chat.
                self._disconnect_endpoint(network_name, current.container_id)
                continue
            if network_name == self.network_for(spec) or self._owned_network_matches(
                network_name, spec
            ):
                self._release_owned_network(
                    network_name,
                    spec,
                    expected_container_id=expected_container_id,
                )
                continue
            # A network that no longer exists cannot be an ownership violation:
            # there is nothing left to release and nothing left to leak. The
            # group bridge is pruned on its own grace timer, so a container that
            # stopped before that timer can outlive its network and keep a stale
            # endpoint record. Reading that record as an unknown network failed
            # this sandbox's stop -- and with it the whole reconcile, and with
            # that the sandbox manager's startup -- over a bridge the platform had
            # already removed itself.
            if self._inspect_network(network_name) is None:
                self._disconnect_endpoint(network_name, current.container_id)
                continue
            raise SandboxError(
                f"retained chat is attached to network {network_name!r}, which is not its "
                "group bridge; a sandbox from before the single-network change needs the "
                "one-time cleanup"
            )
        released = self._containers._require(spec)
        if expected_container_id is not None and released.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during network release")
        return released

    @staticmethod
    def _network_is_stale_for_prune(network: dict[str, Any]) -> bool:
        """Return true only after Docker's creation timestamp clears the race window."""
        raw_created = network.get("Created")
        if not isinstance(raw_created, str) or not raw_created:
            return False
        try:
            created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
        except ValueError:
            return False
        if created.tzinfo is None:
            return False
        age = (datetime.now(UTC) - created.astimezone(UTC)).total_seconds()
        return age >= CHAT_NETWORK_PRUNE_GRACE_S

    def prune_orphan_networks(self, specs: builtins.list[ChatSandboxSpec]) -> None:
        """Remove stale owned bridges without racing a create or activation."""
        result = self._containers._run(
            [
                self.backend.docker_binary,
                "network",
                "ls",
                "--quiet",
                "--filter",
                f"label={CHAT_NETWORK_LABEL}=true",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("group network listing failed: " + result.stderr.strip()[:300])
        by_identity = {(spec.sandbox_id, str(spec.generation)): spec for spec in specs}
        prefix = self._network().interface_prefix
        for network_id in result.stdout.split():
            network = self._inspect_network(network_id)
            if network is None:
                continue
            labels = network.get("Labels") or {}
            namespace = labels.get(CHAT_NAMESPACE_LABEL)
            if namespace != self.backend.sandbox_namespace:
                continue
            bridge = str((network.get("Options") or {}).get(CHAT_NETWORK_BRIDGE_OPTION) or "")
            if (
                network.get("Driver") != "bridge"
                or network.get("Internal") is not False
                or not bridge.startswith(prefix)
                or not str(network.get("Name") or "").startswith(f"{CHAT_NETWORK_PREFIX}-")
            ):
                # The per-phase topology this deployment replaced. It is
                # reported for the one-time cleanup, never adopted or repaired.
                logger.warning(
                    "chat_legacy_network_found",
                    extra={"network": str(network.get("Name") or network_id)},
                )
                continue
            identity = (
                str(labels.get(CHAT_NETWORK_SANDBOX_ID_LABEL) or ""),
                str(labels.get(CHAT_NETWORK_GENERATION_LABEL) or ""),
            )
            spec = by_identity.get(identity)
            if spec is not None:
                sandbox = self._containers.inspect(spec.container_name)
                if sandbox is not None and str(network["Name"]) in sandbox.networks:
                    # This is the live bridge of a retained container, not an
                    # orphan. reconcile() also runs for status polling, so
                    # pruning it here would silently disconnect an active turn
                    # from both relays and the internet.
                    continue
                if not self._network_is_stale_for_prune(network):
                    # Runner and sandbox manager use separate Docker clients. During
                    # create or wake the bridge exists briefly before the
                    # sandbox endpoint is attached.
                    continue
                self._release_owned_network(
                    str(network["Name"]),
                    spec,
                    only_if_sandbox_unattached=True,
                    expected_network_id=str(network.get("Id") or "") or None,
                )
                continue

            # No retained container on this sandbox manager owns the labelled bridge.
            # A runner can create the bridge just before its new container
            # appears in the daemon-wide list, so absence alone is not enough
            # proof that the bridge is orphaned.
            if not self._network_is_stale_for_prune(network):
                continue
            self._release_orphan_network(network)

    def _endpoint_is_current(
        self,
        current: ChatSandboxInspection,
        network_name: str,
        network: dict[str, Any] | None = None,
    ) -> bool:
        """Distinguish a live/pending endpoint from Docker's stale stopped metadata."""
        if network_name not in current.networks:
            return False
        endpoint_ids = dict(current.network_ids)
        recorded_id = endpoint_ids.get(network_name)
        if recorded_id is None:
            # Compatibility for synthetic embeddings and pre-field test
            # inspections. Real Docker inspections always include this entry.
            return True
        if recorded_id == "":
            # Docker records a stopped container's pending connection by name
            # and fills NetworkID only when the container starts.
            return True
        if network is None:
            network = self._inspect_network(network_name)
        if network is None:
            return False
        current_id = str(network.get("Id") or "")
        # A network connected while the container is stopped has no NetworkID
        # until start. A non-empty, different ID is a tombstone left after the
        # old same-name bridge was deleted and must be reconnected explicitly.
        return bool(current_id and recorded_id == current_id)

    def _attach_to_group_network(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        snapshot: _GroupNetworkSnapshot | None = None,
    ) -> ChatSandboxInspection:
        expected = self.network_for(spec)
        if snapshot is not None:
            snapshot.network = None
            snapshot.owners = {}
            snapshot.addresses = None
        sources = tuple(current.networks)
        for source in sources:
            if source in (expected, CHAT_NULL_NETWORK):
                continue
            if source == CHAT_BOOTSTRAP_NETWORK and current.status is ChatSandboxStatus.CREATED:
                continue
            if self._inspect_network(source) is None:
                # A stale endpoint record for a bridge already removed.
                continue
            raise SandboxError(
                f"retained chat is attached to network {source!r}, which is not its group "
                "bridge; a sandbox from before the single-network change needs the "
                "one-time cleanup"
            )
        if current.running:
            # A running Kata guest does not see network changes until it
            # restarts, so a running sandbox is never rewired underneath it.
            raise SandboxError(
                "a running retained chat is not on exactly its group bridge; stop it "
                "before it can be resumed"
            )
        # The relay exceptions wait for the reconciliation after the mutations
        # below: the sandbox is stopped until then, so nothing can send on the
        # bridge, and an exception that is missing meanwhile refuses traffic
        # rather than allowing it. Syncing here too only cost a host-rule
        # helper container on every first start and wake.
        expected = self._ensure_group_network(spec, current=current, sync_exceptions=False)
        if not self._endpoint_is_current(current, expected):
            connected = self._containers._run(
                [
                    self.backend.docker_binary,
                    "network",
                    "connect",
                    expected,
                    current.container_id,
                ],
                timeout=30,
            )
            if connected.returncode != 0:
                refreshed = self._containers._require(spec)
                if not self._endpoint_is_current(refreshed, expected):
                    raise SandboxError(
                        "retained chat could not attach its group network: "
                        + connected.stderr.strip()[:300]
                    )
        for source in sources:
            if source == expected:
                continue
            self._disconnect_endpoint(source, current.container_id)
        moved = self._containers._require(spec)
        if set(moved.networks) != {expected} or not self._endpoint_is_current(moved, expected):
            raise SandboxError("retained chat did not end up on exactly its group bridge")
        # Connect/disconnect may race a relay rollout or bridge replacement.
        # Evidence read before those mutations cannot be handed to the guest:
        # reconcile again and apply the same final check as a hot activation.
        observed = snapshot if snapshot is not None else _GroupNetworkSnapshot()
        self._ensure_group_network(spec, current=moved, snapshot=observed)
        verified = self._verify_group_snapshot(spec, moved, observed)
        if verified is None:
            raise SandboxError("group network changed while the retained chat was attaching")
        return verified

    def _verify_group_snapshot(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        observed: _GroupNetworkSnapshot,
    ) -> ChatSandboxInspection | None:
        """Finish one reconciliation with fresh container and bridge evidence."""
        expected = self.network_for(spec)
        refreshed = self._containers._require(spec)
        if refreshed.container_id != current.container_id or set(refreshed.networks) != {expected}:
            raise SandboxError("retained chat identity or networks changed during activation")
        network = self._inspect_network(expected)
        if network is not None:
            self._verify_group_network(network, spec)
            # A snapshot is useful only if it still names this bridge and its
            # exact peers. Fail a mutation instead of using old addresses.
            endpoints = network.get("Containers") or {}
            if not isinstance(endpoints, dict):
                raise SandboxError("Docker returned invalid group-network endpoints")
            services = set(observed.owners)
            permitted = services | {refreshed.container_id}
            peers = set(map(str, endpoints))
            if (
                observed.network is None
                or network.get("Id") != observed.network.get("Id")
                or not services.issubset(peers)
                or peers - permitted
                or (refreshed.running and refreshed.container_id not in peers)
                or self._relay_addresses_from(observed.owners, network) != observed.addresses
            ):
                raise SandboxError("group network changed during activation")
        if network is not None and self._endpoint_is_current(refreshed, expected, network):
            return refreshed
        return None

    def _require_group_network(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        snapshot: _GroupNetworkSnapshot | None = None,
    ) -> ChatSandboxInspection:
        expected = self.network_for(spec)
        if set(current.networks) == {expected}:
            observed = snapshot if snapshot is not None else _GroupNetworkSnapshot()
            self._ensure_group_network(spec, current=current, snapshot=observed)
            refreshed = self._verify_group_snapshot(spec, current, observed)
            if refreshed is not None:
                return refreshed
        return self._attach_to_group_network(spec, current, snapshot)

    def _verify_guest_network(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        defer_network_release: bool = False,
    ) -> None:
        """Check, from inside the guest, that it sits behind the group's rules.

        The guard proves the host's rules exist; this proves the guest is
        behind them the way the agent will be: a default route, the one
        resolver answering a real query, both relays accepting on their service
        ports, and the host and the metadata address refused by the rules
        themselves. It runs as the sandbox user, so it sees what the agent
        sees. Reaching the public internet is deliberately not a condition: an
        outside outage is not a broken boundary. A failure stops the sandbox,
        which releases the bridge and keeps the workspace.
        """
        network = self._network()
        expected_resolver = (
            str(network.dns_server) if self.backend.runtime == KATA_RUNTIME else "127.0.0.11"
        )
        relays = [[relay.host, relay.port] for relay in network.relays]
        result = self._containers.exec_capture(
            spec,
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                _GUEST_NETWORK_CHECK,
                expected_resolver,
                json.dumps(relays),
            ],
            # The workspace is writable by the repository and every agent turn.
            # Isolated Python from / never imports a planted json.py or
            # socket.py in place of the standard library, so the check can
            # neither be spoofed nor stall start-up behind its timeout.
            workdir="/",
            timeout_s=45,
        )
        try:
            problems = json.loads(result.stdout.decode(errors="replace").strip() or "null")
        except json.JSONDecodeError:
            problems = None
        if result.returncode == 0 and problems == []:
            return
        detail = (
            "; ".join(map(str, problems))
            if isinstance(problems, list)
            else result.stderr.decode(errors="replace").strip()[:300]
        )
        try:
            stopped = self._containers.stop(
                spec,
                expected_container_id=expected_container_id,
                release_networks=not defer_network_release,
            )
        except SandboxError as exc:
            raise SandboxError(
                f"group network check failed inside the sandbox ({detail}), and stopping "
                f"it failed too: {exc}"
            ) from exc
        raise RetainedChatFailStopped(
            f"group network check failed inside the sandbox: {detail}",
            stopped,
        )

    def verify_host_rules(self) -> None:
        """Prove the host still enforces this deployment's rules, repairing them if not.

        Reads the tables from the kernel on every call, so the sweep that
        calls this sees a flushed ruleset on its next pass rather than when
        the guard's last verification ages out; a difference reinstalls and
        reads back. A raise means the host cannot show the boundary every
        running group was started behind.
        """
        self.backend.network_guard().recheck()
