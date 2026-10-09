"""Docker-owned, restartable sandboxes for agent chats.

This module is the storage/lifecycle boundary for retained execution. A chat
sandbox has the following invariants:

* its name and ownership labels are stable for the lifetime of a chat;
* it has no bind or volume carrying workspace/session state;
* ``docker stop`` deactivates it without deleting its writable layer; and
* commands run with ``docker exec`` after an explicit, inspected start.

The control-plane and runner integration is intentionally layered on top of
this class.  Keeping Docker identity checks here means every caller gets the
same fencing and no-mount guarantees instead of rebuilding security-sensitive
argv in several services.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_BINARY,
    GUEST_OBSERVER_EVENTS,
    GUEST_OBSERVER_ROOT,
    GUEST_OBSERVER_ROTATE_BYTES,
    GuestObservation,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverRuntimeError,
    GuestObserverSnapshot,
)
from cloud_agent_host.network_guard import GUARD_RECHECK_S
from cloud_agent_host.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxProcess,
    SandboxSpec,
    TerminalProcess,
)
from cloud_agent_host.tool_windows import MAX_TOOL_MARK_BYTES, TOOL_MARK_FILE, ToolMarkReader
from cloud_agent_shared.operation_profiling import StageObserver, substage

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterator, Sequence
    from typing import BinaryIO

    from cloud_agent_shared.egress import SandboxNetwork


CHAT_SANDBOX_LABEL = "org.freeinference.chat-sandbox"
CHAT_NAMESPACE_LABEL = "org.freeinference.sandbox-namespace"
CHAT_THREAD_LABEL = "org.freeinference.chat-thread-id"
CHAT_SANDBOX_ID_LABEL = "org.freeinference.chat-sandbox-id"
CHAT_GENERATION_LABEL = "org.freeinference.chat-generation"
CHAT_RUNTIME_LABEL = "org.freeinference.chat-runtime"
CHAT_RUNTIME_VERSION_LABEL = "org.freeinference.chat-runtime-version"
CHAT_IMAGE_DIGEST_LABEL = "org.freeinference.chat-image-digest"
CHAT_MEMORY_LIMIT_LABEL = "org.freeinference.chat-memory-limit"
CHAT_CPU_LIMIT_LABEL = "org.freeinference.chat-cpu-limit"
CHAT_STOP_TIMEOUT_LABEL = "org.freeinference.chat-stop-timeout-seconds"
CHAT_SHARED_LABEL = "org.freeinference.chat-shared-live"
CHAT_RESIDENT_CAPACITY_LABEL = "org.freeinference.chat-resident-slot-capacity"
#: Marks a disposable preflight probe with its creation time (Unix seconds).
#: Retained-container discovery skips it: the broker adopted probes as orphan
#: chats and stopped them mid-probe, failing the host's runtime qualification.
CHAT_PROBE_LABEL = "org.freeinference.chat-probe-created"
#: A probe older than this was abandoned by a process killed mid-probe. Since
#: discovery no longer stops it, the next probe in the namespace removes it.
PROBE_ABANDONED_AFTER_S = 3600
CHAT_NETWORK_LABEL = "org.freeinference.chat-sandbox-network"
CHAT_NETWORK_SANDBOX_ID_LABEL = "org.freeinference.chat-sandbox-id"
CHAT_NETWORK_GENERATION_LABEL = "org.freeinference.chat-generation"
CHAT_NETWORK_SERVICES_LABEL = "org.freeinference.chat-service-aliases"
CHAT_NETWORK_POOL_LABEL = "org.freeinference.chat-network-pool"
CHAT_CONTAINER_PREFIX = "freeinference-chat"
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
CHAT_WORKDIR = "/workspace"
#: Where a task's uploaded files land. Outside the worktree on purpose: the
#: publish path stages the workspace with ``git add -A -N``, so anything
#: written under CHAT_WORKDIR is committed and shows up in the draft pull
#: request. Root-owned and read-only to the sandbox — see
#: ``build_upload_archive``, and the Dockerfile that creates the directory.
CHAT_UPLOADS_DIR = "/uploads"
#: ``docker cp`` time allowed per GiB of an imported archive, and the floor for
#: a smaller one: the two minutes a 1 GiB checkout always had, kept in step with
#: a host that raised its source import limit.
ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S = 120.0
CHAT_STATE_DIR = "/agent-state"
ACTIVATION_TMPFS = "/run/agent-activation"
MEMBER_TMPFS_ROOT = "/run/agent-members"
#: This module is a library boundary: it reports what it observed and
#: leaves the decision to the caller, so a warning here is the only
#: record an operator gets of a partially capable guest.
logger = logging.getLogger(__name__)

#: How many times the guest observer probe may be tried before a host is
#: refused. Three, because the probe is a race against a guest that has just
#: booted and the cost of asking again is one throwaway sandbox, while the cost
#: of a false refusal is the whole host advertising no deployment.
GUEST_OBSERVER_PROBE_ATTEMPTS = 3
#: Long enough for the observer's poll loop, its ring drain and its first status
#: publication to get clear of whatever was still starting behind them.
GUEST_OBSERVER_PROBE_RETRY_S = 2.0


class _ObserverProbeRace(SandboxError):
    """A probe failure that a second attempt may not reproduce.

    Raised only by the stages that read back from a running guest observer --
    opening the cursor and closing it again. Everything else the probe checks
    is a property of the image, the kernel or the exec sandbox, and answers
    identically however many times it is asked; those keep raising
    ``SandboxError`` so a genuinely incapable host is refused at once instead of
    three times.

    It is a ``SandboxError`` so that callers which already handle a failed
    preflight are unaffected by the distinction.
    """


MEMBER_UID_BASE = 11001
MEMBER_UID_LIMIT = 128
MEMBER_REAPER_TARGET = "/usr/local/bin/member-process-reaper"
MEMBER_REAPER_SOURCE = "/usr/local/libexec/member-process-reaper"
# Kata exec cannot acquire capabilities outside the container's bounding set.
# Only the fixed guest-root helpers use these: tracing, self-test UID drops,
# socket ownership, and reading/signalling member process receipts.
KATA_HELPER_CAPABILITIES = (
    "BPF",
    "PERFMON",
    "SETUID",
    "SETGID",
    "CHOWN",
    "DAC_OVERRIDE",
    "KILL",
    "SYS_PTRACE",
)
_UNPRIVILEGED_EXEC = ("/usr/bin/setpriv", "--no-new-privs", "--")
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")

# PID 1 contains no chat credential and owns no chat state outside the
# container writable layer. A reconnectable protocol bridge may later replace
# this inert process as a latency optimization; continuity does not depend on
# it. Docker's ``--init`` owns signal forwarding/reaping, while one never-
# respawned child avoids manufacturing a same-UID process event every hour.
# Run inside a guest as the sandbox user after every cold start, with $1 the
# resolver the guest should use and $2 the relays as JSON. Prints the problems
# it found as a JSON list; an empty list is a pass. Every probe has a short
# bound and nothing here leaves the host: the resolver answers one query for the
# root's name servers, and the two boundary probes aim at the host itself and at
# the metadata address, which the host rules must refuse at once with their own
# ICMP "administratively prohibited". A host without the rules would answer
# "port unreachable" for itself instead, and route the other onwards.
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
refused_by_rules(
    'the metadata address 169.254.169.254:80',
    lambda: socket.create_connection(('169.254.169.254', 80), timeout=2).close(),
)
print(json.dumps(problems))
"""

_INIT_COMMAND = (
    'trap \'kill "$child" 2>/dev/null; wait "$child"; exit 0\' TERM INT; '
    'sleep 2147483647 & child=$!; wait "$child"'
)


class ChatSandboxStatus(StrEnum):
    """Docker states relevant to the restartable lifecycle."""

    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    DEAD = "dead"
    UNKNOWN = "unknown"


class RetainedChatLost(SandboxError):
    """The durable chat container is missing or is no longer the same object."""


@dataclass(frozen=True)
class ChatSandboxSpec:
    """Immutable identity and resource policy of one chat container."""

    thread_id: str
    sandbox_id: str
    generation: int
    runtime_id: str
    runtime_version: str | None = None
    sandbox_image_digest: str | None = None
    memory_limit: str = "4g"
    cpu_limit: str = "2"
    stop_timeout_s: int = 10
    shared_live: Literal[True] = True
    resident_slot_capacity: int = 8
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.shared_live is not True:
            raise ValueError("retained chat sandboxes are always shared-live groups")
        for name, value in (
            ("thread_id", self.thread_id),
            ("sandbox_id", self.sandbox_id),
            ("runtime_id", self.runtime_id),
        ):
            if not value or "\x00" in value or len(value) > 256:
                raise ValueError(f"{name} must be a non-empty opaque id of at most 256 bytes")
        if self.runtime_version is not None and (
            not self.runtime_version
            or "\x00" in self.runtime_version
            or len(self.runtime_version) > 128
        ):
            raise ValueError("runtime_version must be a non-empty value of at most 128 bytes")
        if self.sandbox_image_digest is not None and (
            not self.sandbox_image_digest
            or "\x00" in self.sandbox_image_digest
            or len(self.sandbox_image_digest) > 512
        ):
            raise ValueError("sandbox_image_digest must be a non-empty value of at most 512 bytes")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if not 1 <= self.stop_timeout_s <= 300:
            raise ValueError("stop_timeout_s must be between 1 and 300")
        if not 2 <= self.resident_slot_capacity <= MEMBER_UID_LIMIT:
            raise ValueError(f"resident_slot_capacity must be between 2 and {MEMBER_UID_LIMIT}")
        reserved = {
            CHAT_SANDBOX_LABEL,
            CHAT_NAMESPACE_LABEL,
            CHAT_THREAD_LABEL,
            CHAT_SANDBOX_ID_LABEL,
            CHAT_GENERATION_LABEL,
            CHAT_RUNTIME_LABEL,
            CHAT_RUNTIME_VERSION_LABEL,
            CHAT_IMAGE_DIGEST_LABEL,
            CHAT_MEMORY_LIMIT_LABEL,
            CHAT_CPU_LIMIT_LABEL,
            CHAT_STOP_TIMEOUT_LABEL,
            CHAT_SHARED_LABEL,
            CHAT_RESIDENT_CAPACITY_LABEL,
        }
        overlap = reserved.intersection(self.labels)
        if overlap:
            raise ValueError(f"reserved chat sandbox label(s): {', '.join(sorted(overlap))}")

    @property
    def container_name(self) -> str:
        """A stable Docker-safe name without exposing a user-controlled id."""
        digest = hashlib.sha256(f"{self.thread_id}\0{self.sandbox_id}".encode()).hexdigest()[:32]
        return f"{CHAT_CONTAINER_PREFIX}-{digest}"

    def docker_labels(self) -> dict[str, str]:
        """Return the immutable Docker ownership and runtime labels."""
        labels = {
            CHAT_SANDBOX_LABEL: "true",
            CHAT_THREAD_LABEL: self.thread_id,
            CHAT_SANDBOX_ID_LABEL: self.sandbox_id,
            CHAT_GENERATION_LABEL: str(self.generation),
            CHAT_RUNTIME_LABEL: self.runtime_id,
            CHAT_MEMORY_LIMIT_LABEL: self.memory_limit,
            CHAT_CPU_LIMIT_LABEL: self.cpu_limit,
            CHAT_STOP_TIMEOUT_LABEL: str(self.stop_timeout_s),
        }
        if self.runtime_version:
            labels[CHAT_RUNTIME_VERSION_LABEL] = self.runtime_version
        if self.sandbox_image_digest:
            labels[CHAT_IMAGE_DIGEST_LABEL] = self.sandbox_image_digest
        labels[CHAT_SHARED_LABEL] = "true"
        labels[CHAT_RESIDENT_CAPACITY_LABEL] = str(self.resident_slot_capacity)
        labels.update(self.labels)
        return labels


@dataclass(frozen=True)
class ChatSandboxInspection:
    """Trusted view of one inspected chat container."""

    container_id: str
    name: str
    status: ChatSandboxStatus
    running: bool
    image: str
    labels: dict[str, str]
    mounts: tuple[dict[str, Any], ...]
    networks: tuple[str, ...]
    network_ids: tuple[tuple[str, str], ...] = ()
    sandbox_runtime: str = ""


@dataclass
class _GroupNetworkSnapshot:
    """One reconciliation's evidence; never retained across commands or starts."""

    network: dict[str, Any] | None = None
    owners: dict[str, list[str]] = field(default_factory=dict)
    addresses: dict[str, str] | None = None


@dataclass
class SandboxActionFacts:
    """What one activation actually did to the container.

    Startup classification needs the action, not a guess from timing: a stop
    followed by a start is a different path from a container that never
    stopped, and the container's state after the fact cannot tell them apart.
    A ``group`` in the supervisor's memory can predate this attempt, so the
    answer has to come from the call that performed the work.

    ``create`` outranks the start that necessarily follows it, because an
    attempt that paid for the container should not be reported as one that
    found it stopped. Every other action is overwritten by the next one, so
    adopting a container that already existed still lets ``start`` report
    whether it had to run ``docker start``.
    """

    container_action: str = "unknown"

    def note(self, action: str) -> None:
        """Record what this activation did, keeping the strongest fact."""
        if self.container_action == "create":
            return
        self.container_action = action


class ChatSandboxManager:
    """Create and operate retained chat containers through Docker only."""

    def __init__(self, backend: ContainerBackend) -> None:
        self.backend = backend
        self._member_reaper_lock = threading.Lock()
        # Multiple fork siblings may admit turns concurrently. Singleton
        # observer startup/rotation and its live cursors therefore need one
        # broker-wide critical section per manager.
        self._guest_observer_lock = threading.RLock()
        self._guest_observer_active: dict[str, dict[int, int]] = {}

    # ── the group network ──────────────────────────────────────────────
    #
    # One routable bridge per sandbox group, used by setup, the agent and every
    # fork member alike, named from the group's identity and guarded on the
    # host by the deployment's rule set (cloud_agent_shared.egress). Docker
    # routes and masquerades it; the host's rules decide what it reaches. The
    # only other endpoints on it are the two relays, copied from the relay
    # network, and the host holds an exact exception for each of them.

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
        result = self._run([self.backend.docker_binary, "network", "inspect", network], timeout=30)
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
        result = self._run(
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
        result = self._kata_privileged_exec(
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
        connected = self._run(command, timeout=30)
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
            sandbox = self.inspect(spec.container_name)
        if sandbox is not None:
            self._verify_identity(sandbox, spec)
            permitted.add(sandbox.container_id)

        required_aliases = set(service_aliases)
        unexpected = sorted(set(map(str, containers)) - permitted)
        peer_aliases = self._network_aliases_for(unexpected, network)
        for container_id in unexpected:
            endpoint_aliases = peer_aliases.get(container_id)
            if not endpoint_aliases or not endpoint_aliases.intersection(required_aliases):
                raise SandboxError(f"group network {network!r} contains an unexpected peer")
            disconnected = self._run(
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
        process-local lock that would not cover the broker and runner replicas.
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
            created = self._run(command, timeout=30)
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

    def _sync_relay_exceptions(self, network: str, addresses: dict[str, str] | None = None) -> None:
        """Make the host's relay exceptions for one bridge match its relays."""
        config = self._network()
        ports = {relay.host: relay.port for relay in config.relays}
        if addresses is None:
            addresses = self.relay_addresses(network)
        self.backend.network_guard().sync_relays(
            config.bridge_name(network),
            {(address, ports[alias]) for alias, address in addresses.items()},
        )

    def _drop_relay_exceptions(self, network_name: str) -> None:
        """Forget a removed bridge's relay exceptions, without failing the removal.

        They match nothing once the bridge is gone, and a later bridge with the
        same name has its own set written when its relays attach. A guard that
        cannot run here fails the next activation instead.
        """
        try:
            self.backend.network_guard().sync_relays(
                self._network().bridge_name(network_name), set()
            )
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
    ) -> _GroupNetworkSnapshot:
        """Attach and verify the relays, then mirror them into the host's rules."""
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
        self._sync_relay_exceptions(network, addresses)
        return _GroupNetworkSnapshot(network=reconciled, owners=owners, addresses=addresses)

    def _ensure_group_network(
        self,
        spec: ChatSandboxSpec,
        *,
        current: ChatSandboxInspection | None = None,
        snapshot: _GroupNetworkSnapshot | None = None,
    ) -> str:
        """Create/reconcile the group bridge, its relays and their exceptions."""
        # Rules first: no group bridge exists on a host that has not proved it
        # enforces them.
        self.backend.network_guard().ensure()
        expected = self.network_for(spec)
        network = self._inspect_network(expected)
        if network is not None and self._network_is_stale_for_prune(network):
            sandbox = current if current is not None else self.inspect(spec.container_name)
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
        result = self._ensure_group_services(expected, spec, sandbox=current)
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
        disconnected = self._run(
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
        sandbox = self.inspect(spec.container_name)
        if sandbox is not None:
            self._verify_identity(sandbox, spec)
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
        removed = self._run(
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
            refreshed_sandbox = self.inspect(spec.container_name)
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
        removed = self._run(
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
    ) -> ChatSandboxInspection:
        """Release the group bridge after the retained container stops."""
        if current.running:
            raise SandboxError("refusing to release networking from a running chat sandbox")
        for network_name in current.networks:
            if network_name == CHAT_NULL_NETWORK:
                # There is no connectivity here to withdraw. Docker reports the
                # null network for a container whose last endpoint has gone, so
                # this is what a release that already succeeded looks like --
                # and reading it as a network nobody owns made every
                # later stop of an already-released sandbox raise. Because the
                # supervisor stops every retained sandbox while reconciling, one
                # such container was enough to keep the broker from starting.
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
                self._release_owned_network(network_name, spec)
                continue
            # A network that no longer exists cannot be an ownership violation:
            # there is nothing left to release and nothing left to leak. The
            # group bridge is pruned on its own grace timer, so a container that
            # stopped before that timer can outlive its network and keep a stale
            # endpoint record. Reading that record as an unknown network failed
            # this sandbox's stop -- and with it the whole reconcile, and with
            # that the broker's startup -- over a bridge the platform had
            # already removed itself.
            if self._inspect_network(network_name) is None:
                self._disconnect_endpoint(network_name, current.container_id)
                continue
            raise SandboxError(
                f"retained chat is attached to network {network_name!r}, which is not its "
                "group bridge; a sandbox from before the single-network change needs the "
                "one-time cleanup"
            )
        return self._require(spec)

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
        result = self._run(
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
                sandbox = self.inspect(spec.container_name)
                if sandbox is not None and str(network["Name"]) in sandbox.networks:
                    # This is the live bridge of a retained container, not an
                    # orphan. reconcile() also runs for status polling, so
                    # pruning it here would silently disconnect an active turn
                    # from both relays and the internet.
                    continue
                if not self._network_is_stale_for_prune(network):
                    # Runner and broker use separate Docker clients. During
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

            # No retained container on this broker owns the labelled bridge.
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
        expected = self._ensure_group_network(spec, current=current)
        if not self._endpoint_is_current(current, expected):
            connected = self._run(
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
                refreshed = self._require(spec)
                if not self._endpoint_is_current(refreshed, expected):
                    raise SandboxError(
                        "retained chat could not attach its group network: "
                        + connected.stderr.strip()[:300]
                    )
        for source in sources:
            if source == expected:
                continue
            self._disconnect_endpoint(source, current.container_id)
        moved = self._require(spec)
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
        refreshed = self._require(spec)
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

    def _verify_guest_network(self, spec: ChatSandboxSpec) -> None:
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
        result = self.exec_capture(
            spec,
            ["python3", "-c", _GUEST_NETWORK_CHECK, expected_resolver, json.dumps(relays)],
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
            self.stop(spec)
        except SandboxError as exc:
            raise SandboxError(
                f"group network check failed inside the sandbox ({detail}), and stopping "
                f"it failed too: {exc}"
            ) from exc
        raise SandboxError(f"group network check failed inside the sandbox: {detail}")

    def verify_host_rules(self) -> None:
        """Prove the host still enforces this deployment's rules, repairing them if not.

        Cheap while the guard's last verification is fresh; otherwise it
        reinstalls and reads the tables back. A raise means the host cannot
        show the boundary every running group was started behind.
        """
        self.backend.network_guard().ensure()

    def require_docker_data_root(self, expected: str) -> str:
        """Verify retained state is owned by the intended Docker data root.

        The broker intentionally cannot read or mount this host path. Docker
        reports its daemon-side root over the socket; comparing that value is
        the configuration fence that catches a host accidentally using its
        ephemeral system disk instead of the operator's durable Docker disk.
        Whether that path is a mounted durable device remains a host bootstrap
        check (``findmnt``), because a container with only the Docker socket
        must not be given the host filesystem merely to prove it.
        """
        if not expected or "\x00" in expected or not os.path.isabs(expected):
            raise ValueError("expected Docker data root must be an absolute host path")
        result = self._run(
            [self.backend.docker_binary, "info", "--format", "{{.DockerRootDir}}"],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("Docker data-root inspection failed: " + result.stderr.strip()[:300])
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1 or not os.path.isabs(lines[0]):
            raise SandboxError("Docker returned an invalid data-root path")
        actual = os.path.normpath(lines[0])
        configured = os.path.normpath(expected)
        if actual != configured:
            raise SandboxError(
                f"Docker data root is {actual!r}, expected retained storage at {configured!r}"
            )
        return actual

    def build_create_command(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
    ) -> builtins.list[str]:
        """Compose a no-secret, no-chat-volume ``docker create`` command."""
        self._validate_extra_args()
        if (
            image is not None
            and spec.sandbox_image_digest is not None
            and image != spec.sandbox_image_digest
        ):
            raise SandboxError("chat sandbox image override does not match its immutable digest")
        argv = [
            self.backend.docker_binary,
            "create",
            "--name",
            spec.container_name,
            "--init",
        ]
        labels = {
            **spec.docker_labels(),
            CHAT_NAMESPACE_LABEL: self.backend.sandbox_namespace,
        }
        for key, value in sorted(labels.items()):
            argv += ["--label", f"{key}={value}"]
        argv += [
            "--workdir",
            CHAT_WORKDIR,
            "--user",
            f"{self.backend.uid}:{self.backend.gid}",
            # Docker retains the create-time network as immutable primary
            # metadata even after disconnect. Use its daemon-stable bridge as
            # a dormant anchor, then attach the group bridge and disconnect
            # this one while the container is still stopped. No untrusted code
            # runs until start() verifies the group bridge is the sole network.
            "--network",
            CHAT_BOOTSTRAP_NETWORK,
            # Docker resolves relay aliases through 127.0.0.11 and forwards
            # every other name to this server from the container's namespace:
            # the deployment's one resolver, the only DNS the host's rules let
            # a group reach. A Kata guest is given the same server directly.
            "--dns",
            (
                str(self.backend.sandbox_network.dns_server)
                if self.backend.sandbox_network is not None
                else CHAT_OFFLINE_DNS
            ),
            "--memory",
            spec.memory_limit,
            "--cpus",
            spec.cpu_limit,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--stop-timeout",
            str(spec.stop_timeout_s),
            "--tmpfs",
            (
                f"{ACTIVATION_TMPFS}:rw,nosuid,nodev,noexec,mode=0700,"
                f"uid={self.backend.uid},gid={self.backend.gid}"
            ),
        ]
        for slot_id in range(spec.resident_slot_capacity):
            uid = MEMBER_UID_BASE + slot_id
            argv += [
                "--tmpfs",
                (
                    f"{MEMBER_TMPFS_ROOT}/{slot_id}:rw,nosuid,nodev,noexec,"
                    f"mode=0700,uid={uid},gid={self.backend.gid}"
                ),
            ]
        if self.backend.runtime == KATA_RUNTIME:
            # The eBPF program is loaded by a trusted root helper *inside* the
            # Kata guest.  Its journal is root-only and ephemeral; agent UIDs
            # cannot read, forge, or retain it across a guest restart.
            #
            # Kata limits even privileged exec to this bounding set. The
            # ordinary member exec path explicitly sets no-new-privileges;
            # Kata does not inherit PID 1's setting for docker exec.
            for capability in KATA_HELPER_CAPABILITIES:
                argv += ["--cap-add", capability]
            argv += [
                "--tmpfs",
                f"{GUEST_OBSERVER_ROOT}:rw,nosuid,nodev,noexec,mode=0700,uid=0,gid=0",
            ]
        if self.backend.runtime:
            argv += ["--runtime", self.backend.runtime]
        argv += self.backend.extra_args
        argv += [
            "--entrypoint",
            "/bin/sh",
            image or spec.sandbox_image_digest or self.backend.image,
            "-c",
            _INIT_COMMAND,
        ]
        return argv

    def create(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
        facts: SandboxActionFacts | None = None,
        observer: StageObserver | None = None,
    ) -> ChatSandboxInspection:
        """Create one chat container idempotently, never replacing an existing one."""
        with substage(observer, "network_ensure"):
            self._ensure_group_network(spec)
        existing = self.inspect(spec.container_name)
        if existing is not None:
            # Idempotent adoption, not a creation. No action is noted here:
            # the start() that follows knows whether this attempt had to run
            # `docker start`, and that is what classifies the path.
            self._verify_identity(existing, spec)
            return self._require_group_network(spec, existing)
        command = self.build_create_command(spec, image=image)
        with substage(observer, "container_create"):
            created = self._run(command, timeout=120)
        if created.returncode != 0:
            # A racing replica may have won after our inspect.  Only accept the
            # resulting container after checking every ownership label.
            raced = self.inspect(spec.container_name)
            if raced is None:
                raise SandboxError(
                    "chat sandbox could not be created: " + created.stderr.strip()[:300]
                )
            # The other replica paid for the creation, so this attempt did
            # not create the container. `start` reports what it did with it.
            self._verify_identity(raced, spec)
            return self._require_group_network(spec, raced)
        if facts is not None:
            # The literal the startup-path classifier reads. Emitting anything
            # else -- "created", say -- hid every brand-new group: the guard in
            # ``SandboxActionFacts.note`` would not recognise it either, so the
            # ``start`` that always follows overwrote it, and a container this
            # attempt really paid to create was reported as a wake from
            # stopped. Two of the four measured paths became indistinguishable.
            facts.note("create")
        inspected = self.inspect(spec.container_name)
        if inspected is None:
            raise SandboxError("Docker created the chat sandbox but it cannot be inspected")
        self._verify_identity(inspected, spec)
        return self._require_group_network(spec, inspected)

    def require_existing(
        self,
        spec: ChatSandboxSpec,
        *,
        container_id: str,
    ) -> ChatSandboxInspection:
        """Adopt only the exact durable container; never recreate missing chat state."""
        if not container_id:
            raise ValueError("expected retained container id must be non-empty")
        try:
            current = self._require(spec)
        except SandboxError as exc:
            raise RetainedChatLost("retained chat container is missing or invalid") from exc
        if current.container_id != container_id:
            raise RetainedChatLost(
                "retained chat container id does not match its durable sandbox record"
            )
        return current

    def inspect(
        self, container: str, *, verify_mounts: bool = True
    ) -> ChatSandboxInspection | None:
        """Inspect a container; missing is ``None``, daemon/protocol failures raise."""
        result = self._run(
            [self.backend.docker_binary, "container", "inspect", container], timeout=30
        )
        if result.returncode != 0:
            lowered = result.stderr.lower()
            if "no such container" in lowered or "no such object" in lowered:
                return None
            raise SandboxError("chat sandbox inspect failed: " + result.stderr.strip()[:300])
        try:
            payload = json.loads(result.stdout)
            raw = payload[0]
            config = raw.get("Config") or {}
            labels = config.get("Labels") or {}
            mounts = raw.get("Mounts") or []
            state = raw.get("State") or {}
            network_settings = (raw.get("NetworkSettings") or {}).get("Networks") or {}
            networks = network_settings.keys()
            status = self._status(state)
            inspection = ChatSandboxInspection(
                container_id=str(raw["Id"]),
                name=str(raw.get("Name") or "").removeprefix("/"),
                status=status,
                running=bool(state.get("Running")),
                # Config.Image is the exact reference passed to ``docker
                # create``. ``Image`` is Docker's resolved local layer id and
                # cannot be compared directly with a registry repo digest.
                image=str(config.get("Image") or raw.get("Image") or ""),
                labels={str(key): str(value) for key, value in labels.items()},
                mounts=tuple(dict(mount) for mount in mounts),
                networks=tuple(sorted(str(network) for network in networks)),
                network_ids=tuple(
                    sorted(
                        (
                            str(network),
                            str((endpoint or {}).get("NetworkID") or ""),
                        )
                        for network, endpoint in network_settings.items()
                    )
                ),
                sandbox_runtime=str((raw.get("HostConfig") or {}).get("Runtime") or ""),
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxError("Docker returned an invalid chat sandbox inspection") from exc
        if verify_mounts:
            self._verify_mounts(inspection)
        return inspection

    def _belongs_to_namespace(self, inspection: ChatSandboxInspection) -> bool:
        """Fence daemon-wide discovery to this independently deployed stack.

        Ownership is the immutable namespace label and nothing else. Containers
        without one predate that label and the single-network change; they are
        left for the one-time cleanup rather than adopted by guesswork.
        """
        return inspection.labels.get(CHAT_NAMESPACE_LABEL) == self.backend.sandbox_namespace

    def list(self) -> builtins.list[ChatSandboxInspection]:
        """List retained containers owned by this deployment namespace."""
        result = self._run(
            [
                self.backend.docker_binary,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                '{"id":{{json .ID}},"namespace":{{json (.Label "'
                + CHAT_NAMESPACE_LABEL
                + '")}},"probe":{{json (.Label "'
                + CHAT_PROBE_LABEL
                + '")}}}',
                "--filter",
                f"label={CHAT_SANDBOX_LABEL}=true",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox listing failed: " + result.stderr.strip()[:300])
        found: builtins.list[ChatSandboxInspection] = []
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
                container_id, namespace = row["id"], row["namespace"]
                probe = row.get("probe") or ""
                if not isinstance(container_id, str) or not container_id:
                    raise ValueError("missing container id")
                if not isinstance(namespace, str) or not isinstance(probe, str):
                    raise ValueError("invalid labels")
            except (KeyError, TypeError, ValueError) as exc:
                raise SandboxError("Docker returned an invalid chat sandbox listing") from exc
            # Read ownership from ls before making a per-container RPC: a
            # wedged foreign Kata shim can block even docker inspect. A probe
            # belongs to the process running it and is never a chat to adopt.
            if namespace != self.backend.sandbox_namespace or probe:
                continue
            # A foreign namespace cannot make this broker fail startup by
            # carrying policy-invalid mounts. Apply this stack's mount fence
            # only after ownership is established.
            item = self.inspect(container_id, verify_mounts=False)
            if item is not None and self._belongs_to_namespace(item):
                self._verify_mounts(item)
                found.append(item)
        return found

    def start(
        self,
        spec: ChatSandboxSpec,
        *,
        facts: SandboxActionFacts | None = None,
        observer: StageObserver | None = None,
    ) -> ChatSandboxInspection:
        """Start the exact stopped container and verify it reached RUNNING.

        A cold start is also where the network is proved from inside: nothing
        from the repository runs in a guest that has not passed
        :meth:`_verify_guest_network`.
        """
        current = self._require(spec)
        network_snapshot = _GroupNetworkSnapshot()
        with substage(observer, "group_network"):
            current = self._require_group_network(spec, current, network_snapshot)
        if current.running:
            if facts is not None:
                facts.note("reuse")
            with substage(observer, "guest_network_sync"):
                self._sync_kata_guest_network(spec, current, network_snapshot.addresses)
            return current
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("a paused chat sandbox is not a valid stopped activation")
        if current.status is ChatSandboxStatus.DEAD:
            raise SandboxError("a dead chat sandbox cannot be resumed")
        with substage(observer, "container_start"):
            result = self._run(
                [self.backend.docker_binary, "start", current.container_id], timeout=120
            )
        if facts is not None:
            facts.note("start")
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be started: " + result.stderr.strip()[:300])
        started = self._require(spec)
        if not started.running or started.status is not ChatSandboxStatus.RUNNING:
            raise SandboxError(
                f"chat sandbox started into unexpected state {started.status.value!r}"
            )
        with substage(observer, "guest_network_sync"):
            self._sync_kata_guest_network(spec, started)
        with substage(observer, "guest_network_check"):
            self._verify_guest_network(spec)
        return started

    def stop(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        """Deactivate, release private networking, and retain the writable layer."""
        current = self._require(spec)
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("refusing to treat a paused chat sandbox as cleanly stopped")
        if not current.running:
            return self._release_sandbox_networks(spec, current)
        result = self._run(
            [
                self.backend.docker_binary,
                "stop",
                "--time",
                str(spec.stop_timeout_s),
                current.container_id,
            ],
            timeout=spec.stop_timeout_s + 30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be stopped: " + result.stderr.strip()[:300])
        stopped = self._require(spec)
        if stopped.running or stopped.status not in {
            ChatSandboxStatus.STOPPED,
            ChatSandboxStatus.CREATED,
        }:
            raise SandboxError(
                f"chat sandbox stopped into unexpected state {stopped.status.value!r}"
            )
        return self._release_sandbox_networks(spec, stopped)

    def require_network(
        self, spec: ChatSandboxSpec, *, observer: StageObserver | None = None
    ) -> None:
        """Prove this container is on exactly its guarded group bridge.

        Called before every terminal and broker workspace operation, and by a
        turn's commands once its last proof is a minute old, so it is cheap: the
        guard's last verification is reused for a minute, and the relays and
        their host exceptions are reconciled against what Docker reports now.
        """
        snapshot = _GroupNetworkSnapshot()
        with substage(observer, "network_ensure"):
            current = self._require_group_network(spec, self._require(spec), snapshot)
        expected = self.network_for(spec)
        if set(current.networks) != {expected}:
            raise SandboxError(
                f"retained chat network is {list(current.networks)!r}, not its group "
                f"bridge {expected!r}"
            )
        with substage(observer, "guest_network_sync"):
            self._sync_kata_guest_network(spec, current, snapshot.addresses)

    def build_exec_command(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        interactive: bool = False,
        tty: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
    ) -> builtins.list[str]:
        """Compose ``docker exec`` after identity/running checks at spawn time."""
        if not argv:
            raise ValueError("a retained sandbox exec needs an argv")
        command = [self.backend.docker_binary, "exec"]
        if interactive:
            command.append("--interactive")
        if tty:
            command.append("--tty")
        command += [
            "--user",
            f"{user_uid if user_uid is not None else self.backend.uid}:"
            f"{user_gid if user_gid is not None else self.backend.gid}",
            "--workdir",
            workdir,
        ]
        for key, value in sorted((env or {}).items()):
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError(f"invalid environment entry {key!r}")
            command += ["--env", f"{key}={value}"]
        command.append(spec.container_name)
        if self.backend.runtime == KATA_RUNTIME:
            command += list(_UNPRIVILEGED_EXEC)
            if not interactive:
                # Kata leaves an unattached stdin pipe open. Give commands
                # that have no input a real EOF, without interpolating argv.
                command += ["/bin/sh", "-c", 'exec "$@" </dev/null', "sh"]
        command += argv
        return command

    def spawn(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        live_input: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
        process_pidfile: str | None = None,
    ) -> SandboxProcess:
        """Run an attached command inside the retained container."""
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        exec_argv = argv
        if process_pidfile is not None:
            self._validate_member_pidfile(spec, process_pidfile, user_uid=user_uid)
            # The CLI is a session leader, so its complete child tree can be
            # interrupted without stopping sibling sessions in this container.
            # Persist both PID and Linux start time: a stale numeric PID must
            # never be allowed to signal a later, unrelated process.
            wrapper = (
                'pidfile="$1"; shift; umask 0002; '
                'cleanup() { rm -f -- "$pidfile"; }; '
                "trap cleanup EXIT; "
                "setsid --fork --wait /bin/sh -c '"
                'pidfile="$1"; shift; '
                'fields=$(sed "s/^[^)]*) //" "/proc/$$/stat") || exit 126; '
                # Read field 22 (start time; field 20 after stripping pid and
                # comm) in a subshell.  Doing ``set -- $fields`` in this shell
                # would overwrite the CLI argv that must remain in ``$@``.
                'started=$(set -- $fields; printf "%s" "${20:-}"); '
                'case "$started" in ""|*[!0-9]*) exit 126;; esac; '
                'printf "%s %s\\n" "$$" "$started" > "$pidfile" || exit 126; '
                'exec "$@"\' sh "$pidfile" "$@"'
            )
            exec_argv = ["/bin/sh", "-c", wrapper, "sh", process_pidfile, *argv]
        command = self.build_exec_command(
            spec,
            exec_argv,
            env=env,
            workdir=workdir,
            interactive=live_input,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        process = subprocess.Popen(
            command,
            env={**os.environ},
            stdin=subprocess.PIPE if live_input else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return _RetainedExecProcess(
            process,
            manager=self,
            spec=spec,
            process_pidfile=process_pidfile,
            user_uid=user_uid,
            user_gid=user_gid,
        )

    def _validate_member_pidfile(
        self,
        spec: ChatSandboxSpec,
        pidfile: str,
        *,
        user_uid: int | None,
    ) -> int:
        """Validate one shared member or legacy retained agent PID path."""
        member_match = re.fullmatch(
            rf"{re.escape(MEMBER_TMPFS_ROOT)}/([0-9]+)/resident-[0-9a-f]{{32}}\.pid",
            pidfile,
        )
        if member_match is None:
            raise ValueError("process-scoped kill requires a shared member PID file")
        slot_id = int(member_match.group(1))
        if slot_id >= spec.resident_slot_capacity:
            raise ValueError("resident PID file is outside sandbox capacity")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident PID file does not match its member uid")
        return slot_id

    def ensure_member_process_reaper(self, spec: ChatSandboxSpec) -> None:
        """Install the immutable helper into retained pre-upgrade containers."""
        with self._member_reaper_lock:
            probe = self.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    'test "$(stat -c %u:%g:%a "$1" 2>/dev/null)" = 0:0:555',
                    "sh",
                    MEMBER_REAPER_TARGET,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=0,
                user_gid=0,
            )
            if probe.returncode == 0:
                return
            current = self._require(spec)
            if not current.running:
                raise SandboxError("chat sandbox must be started before helper installation")
            source = os.environ.get("AGENT_MEMBER_PROCESS_REAPER_SOURCE", MEMBER_REAPER_SOURCE)
            if not os.path.isfile(source):
                raise SandboxError(
                    "retained sandbox lacks member-process-reaper and the trusted "
                    f"runner source {source!r} is unavailable"
                )
            temporary = f"{MEMBER_REAPER_TARGET}.install-{secrets.token_hex(8)}"
            copied = self._run(
                [
                    self.backend.docker_binary,
                    "cp",
                    source,
                    f"{current.container_id}:{temporary}",
                ],
                timeout=30,
            )
            if copied.returncode != 0:
                raise SandboxError(
                    "member-process-reaper compatibility install failed: "
                    + copied.stderr.strip()[:300]
                )
            installed = self.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    'chown 0:0 "$1" && chmod 0555 "$1" && mv -f "$1" "$2"',
                    "sh",
                    temporary,
                    MEMBER_REAPER_TARGET,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=0,
                user_gid=0,
            )
            if installed.returncode != 0:
                with contextlib.suppress(Exception):
                    self.exec_capture(
                        spec,
                        ["/bin/rm", "-f", temporary],
                        workdir="/",
                        timeout_s=10,
                        user_uid=0,
                        user_gid=0,
                    )
                raise SandboxError(
                    "member-process-reaper compatibility activation failed: "
                    + installed.stderr.decode("utf-8", "replace")[:300]
                )

    def ensure_codex_mcp_policy(self, spec: ChatSandboxSpec, base_url: str) -> None:
        """Allow only this deployment's relay before starting a Codex member."""
        from cloud_agent_host.codex_mcp import INSTALL_POLICY, relay_endpoint

        endpoint = relay_endpoint(base_url)
        result = self.exec_capture(
            spec,
            ["/usr/bin/python3", "-I", "-c", INSTALL_POLICY, endpoint],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if result.returncode != 0:
            raise SandboxError("could not install the isolated Codex MCP relay policy")

    def ensure_kilo_mcp_runtime(self, spec: ChatSandboxSpec, version: str) -> None:
        """Bring retained Kilo members onto the deployment's isolated runtime."""
        from cloud_agent_host.kilo_mcp import ensure_kilo_runtime

        with self._member_reaper_lock:
            ensure_kilo_runtime(self, spec, version)

    def ensure_mcp_helpers(self, spec: ChatSandboxSpec, runtime: str) -> None:
        """Keep existing conversations on the same adapters as new conversations."""
        from cloud_agent_host.mcp_helpers import install_mcp_helpers

        with self._member_reaper_lock:
            install_mcp_helpers(self, spec, runtime)

    def reap_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Kill unattached live CLI trees in exactly one private member slot."""
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        directory = f"{MEMBER_TMPFS_ROOT}/{slot_id}"
        self.reap_member_processes_except(
            spec,
            slot_id=slot_id,
            user_uid=user_uid,
            user_gid=user_gid,
            baseline=(),
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", f"rm -f -- {directory}/resident-*.pid"],
            workdir="/",
            timeout_s=20,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "stale resident process cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def capture_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> tuple[tuple[int, int], ...]:
        """Capture the reviewed resident PID/start-time set before one turn."""
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=user_uid)
        self.ensure_member_process_reaper(spec)
        result = self.exec_capture(
            spec,
            [MEMBER_REAPER_TARGET, "capture", str(user_uid)],
            workdir="/",
            timeout_s=20,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member process census failed: " + result.stderr.decode("utf-8", "replace")[:300]
            )
        return self._parse_member_processes(result.stdout)

    def reap_member_processes_except(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
        baseline: tuple[tuple[int, int], ...],
    ) -> None:
        """Reap every member process not present in a fenced baseline.

        This is the activation boundary for background tools. It runs as the
        member UID (the container has no CAP_KILL), validates Linux start times
        to defeat PID reuse, and repeats through SIGKILL so a process cannot
        escape merely by changing process groups or forking during cleanup.
        """
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=user_uid)
        self.ensure_member_process_reaper(spec)
        payload = self._encode_member_processes(baseline)
        result = self.exec_capture(
            spec,
            [MEMBER_REAPER_TARGET, "reap", str(user_uid)],
            workdir="/",
            timeout_s=30,
            input_bytes=payload,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member activation process cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    @staticmethod
    def _validate_member_identity(
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
    ) -> None:
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")

    @staticmethod
    def _parse_member_processes(payload: bytes) -> tuple[tuple[int, int], ...]:
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("member process census is not ASCII") from exc
        found: builtins.list[tuple[int, int]] = []
        seen: set[int] = set()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 2 or any(
                not field.isascii() or not field.isdigit() for field in fields
            ):
                raise SandboxError("member process census is malformed")
            pid, started = (int(field) for field in fields)
            if pid <= 1 or started <= 0 or pid in seen or len(found) >= 4096:
                raise SandboxError("member process census is outside its identity fence")
            seen.add(pid)
            found.append((pid, started))
        return tuple(sorted(found))

    @staticmethod
    def _encode_member_processes(processes: tuple[tuple[int, int], ...]) -> bytes:
        canonical = ChatSandboxManager._parse_member_processes(
            "".join(f"{pid} {started}\n" for pid, started in processes).encode("ascii")
        )
        return "".join(f"{pid} {started}\n" for pid, started in canonical).encode("ascii")

    def prepare_shared_workspace(self, spec: ChatSandboxSpec) -> None:
        """Make the imported owner tree writable through the shared group."""
        script = (
            "set -eu; "
            f"find {CHAT_WORKDIR} {CHAT_STATE_DIR} -xdev -type d "
            "-exec chmod g+rwx,g+s -- {} +; "
            f"find {CHAT_WORKDIR} {CHAT_STATE_DIR} -xdev -type f "
            "-exec chmod g+rw -- {} +; "
            f"git -C {CHAT_WORKDIR} config core.sharedRepository group"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script],
            workdir="/",
            timeout_s=120,
        )
        if result.returncode != 0:
            raise SandboxError(
                "shared workspace permission preparation failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def normalize_member_files(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Expose one member's credential-free workspace/state writes to peers."""
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        # Codex, OpenCode, and Kilo each need a member-private provider home:
        # Codex can make temp files owner-only, while the HTTP runtimes keep
        # SQLite databases and per-project process locks. A sibling UID must
        # never traverse those homes merely to normalize its own writes.
        # Claude's sessions/ is a process registry, not its projects/ JSONL
        # history. The CLI resets that registry to 0700 on every start/resume.
        # Leave it private even for its owner: widening it here would race a
        # sibling's startup and make settlement fail after a successful reply.
        # Normalize shared roots while pruning every private-home collection,
        # then enter only direct children owned by this member.
        script = (
            "set -eu; uid=$1; "
            'normalize_dirs() { find "$@" -xdev -uid "$uid" -type d '
            "-exec chmod g+rwx,g+s -- {} +; }; "
            'normalize_files() { find "$@" -xdev -uid "$uid" -type f '
            "-exec chmod g+rw -- {} +; }; "
            f"normalize_dirs {CHAT_WORKDIR}; normalize_files {CHAT_WORKDIR}; "
            f"find {CHAT_STATE_DIR} -xdev "
            f"\\( -path {CHAT_STATE_DIR}/.platform "
            f"-o -path {CHAT_STATE_DIR}/claude/sessions "
            f"-o -path {CHAT_STATE_DIR}/codex-members "
            f"-o -path {CHAT_STATE_DIR}/opencode-members "
            f"-o -path {CHAT_STATE_DIR}/kilo-members \\) -prune -o "
            '-uid "$uid" -type d -exec chmod g+rwx,g+s -- {} +; '
            f"find {CHAT_STATE_DIR} -xdev "
            f"\\( -path {CHAT_STATE_DIR}/.platform "
            f"-o -path {CHAT_STATE_DIR}/claude/sessions "
            f"-o -path {CHAT_STATE_DIR}/codex-members "
            f"-o -path {CHAT_STATE_DIR}/opencode-members "
            f"-o -path {CHAT_STATE_DIR}/kilo-members \\) -prune -o "
            '-uid "$uid" -type f -exec chmod g+rw -- {} +; '
            f"for collection in {CHAT_STATE_DIR}/codex-members "
            f"{CHAT_STATE_DIR}/opencode-members {CHAT_STATE_DIR}/kilo-members; do "
            'test -d "$collection" || continue; '
            'for member in "$collection"/*; do '
            'test -e "$member" || continue; '
            'test "$(stat -c %u "$member" 2>/dev/null || :)" = "$uid" || continue; '
            'normalize_dirs "$member"; normalize_files "$member"; '
            "done; done"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", str(user_uid)],
            workdir="/",
            timeout_s=120,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "shared member permission normalization failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def freeze_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: builtins.list[int],
        token: str,
    ) -> str:
        """Stop every existing shared-workspace writer for one short capture.

        Agent processes use per-member UIDs, while terminals/setup commands
        use the sandbox UID.  Freezing the complete existing UID census closes
        the detached-child gap that a process-group-only barrier leaves.  The
        Git capture exec starts after this census and is the sole new writer;
        admissions remain blocked by the supervisor until thaw completes.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("snapshot freeze token is invalid")
        unique_slots = sorted(set(slot_ids))
        if any(slot < 0 or slot >= spec.resident_slot_capacity for slot in unique_slots):
            raise ValueError("snapshot member slot is outside sandbox capacity")
        manifest = f"{ACTIVATION_TMPFS}/snapshot-{token}.pids"
        target_uids = [self.backend.uid, *(MEMBER_UID_BASE + slot for slot in unique_slots)]
        # The container drops every capability.  Even UID 0 therefore cannot
        # signal a process owned by a member UID; CAP_KILL is intentionally not
        # added back.  Run one census as each identity instead.  Repeating the
        # census until every observed process is stopped closes the race where
        # an idle CLI background process forks while /proc is being scanned.
        script = (
            'set -eu; expected_uid="$1"; committed=0; receipt=""; '
            "resume_receipt() { "
            '[ "$committed" = 0 ] || return 0; '
            "for item in $receipt; do pid=${item%%:*}; started=${item#*:}; "
            'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
            'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'current=$(set -- $fields; printf "%s" "${20:-}"); '
            '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] '
            '&& kill -CONT "$pid" 2>/dev/null || :; done; }; '
            "trap resume_receipt EXIT; trap 'exit 46' HUP INT TERM; round=0; "
            "while :; do round=$((round + 1)); stable=1; "
            "for proc in /proc/[0-9]*; do pid=${proc##*/}; "
            '[ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
            'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
            '[ "$uid" = "$expected_uid" ] || continue; '
            'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'state=$(set -- $fields; printf "%s" "${1:-}"); '
            'started=$(set -- $fields; printf "%s" "${20:-}"); '
            'case "$started" in ""|*[!0-9]*) continue;; esac; item="$pid:$started"; '
            'case " $receipt " in *" $item "*) '
            'case "$state" in T|t) ;; *) kill -STOP "$pid" 2>/dev/null || continue; '
            'stable=0;; esac;; *) kill -STOP "$pid" 2>/dev/null || continue; '
            'receipt="$receipt $item"; stable=0;; esac; done; '
            '[ "$stable" = 1 ] && break; [ "$round" -lt 64 ] || exit 45; done; '
            "for item in $receipt; do pid=${item%%:*}; started=${item#*:}; "
            'printf "%s %s %s\\n" "$pid" "$expected_uid" "$started"; done; '
            "committed=1"
        )
        frozen: builtins.list[tuple[int, int, int]] = []
        try:
            for uid in target_uids:
                result = self.exec_capture(
                    spec,
                    ["/bin/sh", "-c", script, "sh", str(uid)],
                    workdir="/",
                    timeout_s=20,
                    user_uid=uid,
                    user_gid=self.backend.gid,
                )
                if result.returncode != 0:
                    raise SandboxError(
                        "shared snapshot writer freeze failed: "
                        + result.stderr.decode("utf-8", "replace")[:300]
                    )
                frozen.extend(self._parse_frozen_processes(result.stdout, allowed_uids={uid}))
            if len({pid for pid, _uid, _started in frozen}) != len(frozen):
                raise SandboxError("shared snapshot writer census returned a duplicate pid")
            payload = self._encode_frozen_processes(frozen)
            write_script = (
                'set -eu; manifest="$1"; temporary="$manifest.tmp.$$"; umask 077; '
                "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
                'cat > "$temporary"; chmod 600 "$temporary"; mv "$temporary" "$manifest"; '
                "trap - EXIT HUP INT TERM"
            )
            written = self.exec_capture(
                spec,
                ["/bin/sh", "-c", write_script, "sh", manifest],
                workdir="/",
                timeout_s=20,
                input_bytes=payload,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if written.returncode != 0:
                raise SandboxError(
                    "shared snapshot writer manifest failed: "
                    + written.stderr.decode("utf-8", "replace")[:300]
                )
        except BaseException:
            with contextlib.suppress(Exception):
                self._resume_frozen_processes(
                    spec,
                    target_uids=target_uids,
                    frozen=frozen,
                    require_exact_census=False,
                )
            raise
        return manifest

    def thaw_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: builtins.list[int],
        manifest: str,
        require_exact_census: bool = True,
    ) -> None:
        """Resume exactly the frozen PID/start pairs and detect new writers."""
        if not re.fullmatch(
            rf"{re.escape(ACTIVATION_TMPFS)}/snapshot-[0-9a-f]{{32}}\.pids", manifest
        ):
            raise ValueError("snapshot writer manifest path is invalid")
        unique_slots = sorted(set(slot_ids))
        if any(slot < 0 or slot >= spec.resident_slot_capacity for slot in unique_slots):
            raise ValueError("snapshot member slot is outside sandbox capacity")
        target_uids = [self.backend.uid, *(MEMBER_UID_BASE + slot for slot in unique_slots)]
        read = self.exec_capture(
            spec,
            ["/bin/sh", "-c", 'cat -- "$1"', "sh", manifest],
            workdir="/",
            timeout_s=20,
            user_uid=self.backend.uid,
            user_gid=self.backend.gid,
        )
        if read.returncode != 0:
            raise SandboxError(
                "shared snapshot writer manifest is unavailable: "
                + read.stderr.decode("utf-8", "replace")[:300]
            )
        frozen = self._parse_frozen_processes(read.stdout, allowed_uids=set(target_uids))
        resume_error: BaseException | None = None
        try:
            self._resume_frozen_processes(
                spec,
                target_uids=target_uids,
                frozen=frozen,
                require_exact_census=require_exact_census,
            )
        except BaseException as exc:
            resume_error = exc
        removed = self.exec_capture(
            spec,
            ["/bin/sh", "-c", 'rm -f -- "$1"', "sh", manifest],
            workdir="/",
            timeout_s=20,
            user_uid=self.backend.uid,
            user_gid=self.backend.gid,
        )
        if resume_error is not None:
            raise resume_error
        if removed.returncode != 0:
            raise SandboxError(
                "shared snapshot writer manifest cleanup failed: "
                + removed.stderr.decode("utf-8", "replace")[:300]
            )

    @staticmethod
    def _parse_frozen_processes(
        payload: bytes,
        *,
        allowed_uids: set[int],
    ) -> builtins.list[tuple[int, int, int]]:
        """Validate the private PID/start-time receipt before any signal."""
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("shared snapshot writer census is not ASCII") from exc
        frozen: builtins.list[tuple[int, int, int]] = []
        seen: set[int] = set()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 3 or any(
                not field.isascii() or not field.isdigit() for field in fields
            ):
                raise SandboxError("shared snapshot writer census is malformed")
            pid, uid, started = (int(field) for field in fields)
            if pid <= 1 or started <= 0 or uid not in allowed_uids or pid in seen:
                raise SandboxError("shared snapshot writer census is outside its identity fence")
            seen.add(pid)
            frozen.append((pid, uid, started))
        return frozen

    @staticmethod
    def _encode_frozen_processes(frozen: builtins.list[tuple[int, int, int]]) -> bytes:
        """Encode a deterministic, private manifest for the later thaw."""
        return "".join(f"{pid} {uid} {started}\n" for pid, uid, started in sorted(frozen)).encode(
            "ascii"
        )

    def _resume_frozen_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        target_uids: builtins.list[int],
        frozen: builtins.list[tuple[int, int, int]],
        require_exact_census: bool,
    ) -> None:
        """Resume each receipt as its owner and report any changed census."""
        by_uid = {
            uid: [(pid, started) for pid, owner, started in frozen if owner == uid]
            for uid in target_uids
        }
        script = (
            'set -u; exact="$1"; expected_uid="$2"; shift 2; unknown=0; '
            'if [ "$exact" = 1 ]; then for proc in /proc/[0-9]*; do '
            'pid=${proc##*/}; [ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
            'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
            '[ "$uid" = "$expected_uid" ] || continue; '
            'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'started=$(set -- $fields; printf "%s" "${20:-}"); found=0; '
            'for item in "$@"; do [ "$item" = "$pid:$started" ] && found=1; done; '
            '[ "$found" = 1 ] || unknown=1; done; fi; '
            'for item in "$@"; do pid=${item%%:*}; started=${item#*:}; '
            'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
            'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'current=$(set -- $fields; printf "%s" "${20:-}"); '
            '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] '
            '&& kill -CONT "$pid" 2>/dev/null || :; done; [ "$unknown" = 0 ]'
        )
        errors: builtins.list[str] = []
        for uid in target_uids:
            expected = [f"{pid}:{started}" for pid, started in by_uid[uid]]
            try:
                result = self.exec_capture(
                    spec,
                    [
                        "/bin/sh",
                        "-c",
                        script,
                        "sh",
                        "1" if require_exact_census else "0",
                        str(uid),
                        *expected,
                    ],
                    workdir="/",
                    timeout_s=20,
                    user_uid=uid,
                    user_gid=self.backend.gid,
                )
            except BaseException as exc:
                errors.append(str(exc))
                continue
            if result.returncode != 0:
                errors.append(result.stderr.decode("utf-8", "replace")[:300])
        if errors:
            detail = next((error for error in errors if error), "writer census changed")
            raise SandboxError("shared snapshot writer census changed during capture: " + detail)

    def kill_member_process(
        self,
        spec: ChatSandboxSpec,
        *,
        pidfile: str,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Signal one verified resident process group, never the container."""
        self._validate_member_pidfile(spec, pidfile, user_uid=user_uid)
        argv = [
            "/bin/sh",
            "-c",
            self._member_reaper_script('"$2"'),
            "sh",
            str(user_uid),
            pidfile,
        ]
        result = (
            self._kata_privileged_exec(spec, argv, timeout_s=20)
            if self.backend.runtime == KATA_RUNTIME
            else self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=20,
                user_uid=user_uid,
                user_gid=user_gid,
            )
        )
        if result.returncode != 0:
            stderr = (
                result.stderr.decode("utf-8", "replace")
                if isinstance(result.stderr, bytes)
                else str(result.stderr or "")
            )
            raise SandboxError("resident process cleanup failed: " + stderr[:300])

    @staticmethod
    def _member_reaper_script(pidfile_expression: str) -> str:
        """Build the in-container PID/start-time verification and group kill."""
        return (
            'set -eu; expected_uid="$1"; '
            f"for pidfile in {pidfile_expression}; do "
            '[ -e "$pidfile" ] || continue; '
            'read -r pid started extra < "$pidfile" || { rm -f -- "$pidfile"; continue; }; '
            'case "$pid" in ""|*[!0-9]*) rm -f -- "$pidfile"; continue;; esac; '
            'case "$started" in ""|*[!0-9]*) '
            'rm -f -- "$pidfile"; continue;; esac; '
            '[ -z "$extra" ] || { rm -f -- "$pidfile"; continue; }; '
            '[ "$(stat -c %u "/proc/$pid" 2>/dev/null || :)" = "$expected_uid" ] '
            '|| { rm -f -- "$pidfile"; continue; }; '
            'fields=$(sed "s/^[^)]*) //" "/proc/$pid/stat" 2>/dev/null || :); '
            "set -- $fields; current_pgid=${3:-}; current_started=${20:-}; "
            '[ "$current_pgid" = "$pid" ] && [ "$current_started" = "$started" ] '
            '|| { rm -f -- "$pidfile"; continue; }; '
            'kill -TERM -- "-$pid" 2>/dev/null || :; attempt=0; '
            'while kill -0 -- "-$pid" 2>/dev/null; do attempt=$((attempt + 1)); '
            '[ "$attempt" -lt 40 ] || { kill -KILL -- "-$pid" 2>/dev/null || :; '
            'attempt=0; while kill -0 -- "-$pid" 2>/dev/null; do '
            'attempt=$((attempt + 1)); [ "$attempt" -lt 100 ] || exit 124; '
            "sleep 0.05; done; break; }; "
            'sleep 0.05; done; rm -f -- "$pidfile"; '
            "done"
        )

    def spawn_terminal(
        self,
        spec: ChatSandboxSpec,
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        rows: int,
        cols: int,
    ) -> TerminalProcess:
        """Attach a PTY to one shell without creating or killing a container."""
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before terminal exec")
        token = secrets.token_hex(16)
        pidfile = f"{ACTIVATION_TMPFS}/terminal-{token}.pid"
        # The session shell gets its own process group.  Broker suspend/kill
        # then signals this terminal tree only; pausing or killing the retained
        # container would also stop the Agent CLI and violate one-chat
        # lifecycle ownership.
        # Docker's TTY exec already starts this wrapper as the foreground
        # session/process-group leader.  Keep the interactive shell in that
        # group: a background shell observes /dev/null, while another
        # ``setsid`` loses this exec's controlling terminal.  Monitor mode is
        # disabled so commands launched by the shell remain in the signalable
        # group recorded in the pidfile.
        script = (
            'pidfile="$1"; '
            'printf "%s\\n" "$$" > "$pidfile" || exit 126; '
            'cleanup() { rm -f "$pidfile"; }; '
            "trap 'cleanup' EXIT; "
            "umask 0002; "
            '/bin/sh +m -i; status=$?; exit "$status"'
        )
        command = self.build_exec_command(
            spec,
            ["/bin/sh", "-c", script, "sh", pidfile],
            env=env,
            workdir=workdir,
            interactive=True,
            tty=True,
        )
        master_fd, slave_fd = os.openpty()
        try:
            _set_pty_size(slave_fd, rows, cols)
            tty.setraw(slave_fd)
            process = subprocess.Popen(
                command,
                env={**os.environ},
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        return _RetainedTerminalProcess(
            process,
            manager=self,
            spec=spec,
            pidfile=pidfile,
            input_fd=master_fd,
        )

    def reap_stale_terminal_processes(self, spec: ChatSandboxSpec) -> None:
        """Remove PTYs whose broker client disappeared before graceful close.

        Interactive terminal streams are intentionally ephemeral and cannot
        be reattached after a broker process restart.  The retained container
        is durable, however, and Docker may leave an exec process alive after
        its attaching client dies.  Match both the unguessable pidfile and the
        exact wrapper argv before signalling, so a stale/reused numeric PID
        can never target the Agent CLI or PID 1.
        """
        current = self._require(spec)
        if not current.running:
            return
        script = (
            "set -eu; command -v tr >/dev/null; command -v grep >/dev/null; "
            f"for pidfile in {ACTIVATION_TMPFS}/terminal-*.pid; do "
            '[ -e "$pidfile" ] || continue; '
            'pid=$(cat "$pidfile" 2>/dev/null || :); '
            'case "$pid" in ""|*[!0-9]*) rm -f "$pidfile"; continue;; esac; '
            'if [ -r "/proc/$pid/cmdline" ] '
            '&& tr "\\000" "\\n" < "/proc/$pid/cmdline" '
            '| grep -Fqx -- "$pidfile"; then '
            'kill -CONT -"$pid" 2>/dev/null || :; '
            'kill -KILL -"$pid" 2>/dev/null || :; '
            'attempt=0; while kill -0 -"$pid" 2>/dev/null; do '
            'attempt=$((attempt + 1)); test "$attempt" -lt 100 || exit 124; '
            "sleep 0.05; done; "
            'fi; rm -f "$pidfile"; done'
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script],
            workdir="/",
            timeout_s=15,
        )
        if result.returncode != 0:
            raise SandboxError(
                "stale retained terminal cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def exec_capture(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        timeout_s: float = 60,
        input_bytes: bytes | None = None,
        user_uid: int | None = None,
        user_gid: int | None = None,
        observer: StageObserver | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one bounded command and capture bytes without opening a host path.

        ``observer`` times the exec as one stage covering the attach, the
        process start and the command's own run. That is a combination and is
        reported as one: it is never presented as pure disk-read time, and
        splitting it further would need the exec'd helper to report its own
        sub-durations rather than a second exec to probe them.
        """
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        interactive = input_bytes is not None
        if self.backend.runtime == KATA_RUNTIME and not input_bytes:
            # Attaching and immediately closing an empty stdin can wedge the
            # Kata shim. build_exec_command supplies /dev/null in this case.
            interactive = False
        command = self.build_exec_command(
            spec,
            argv,
            env=env,
            workdir=workdir,
            interactive=interactive,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        try:
            with substage(observer, "exec"):
                return subprocess.run(
                    command,
                    env={**os.environ},
                    input=input_bytes,
                    capture_output=True,
                    timeout=timeout_s,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"chat sandbox exec failed: {exc}") from exc

    def _kata_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        detach: bool = False,
        timeout_s: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run one source-controlled platform helper as Kata guest root.

        This is deliberately not a general privileged-exec API.  Callers pass
        only source-controlled observer/cleanup argv from private methods, and
        the runtime guard prevents them from gaining privilege on the host's
        shared kernel under runc.
        """
        if self.backend.runtime != KATA_RUNTIME:
            raise SandboxError("guest-root platform helpers require the Kata runtime")
        current = self._require(spec)
        if current.sandbox_runtime != KATA_RUNTIME:
            raise SandboxError("guest-root platform helpers require an inspected Kata container")
        if not current.running:
            raise SandboxError("chat sandbox must be started before guest observation")
        command = [self.backend.docker_binary, "exec"]
        if detach:
            command.append("--detach")
        command += [
            "--privileged",
            "--user",
            "0:0",
            "--workdir",
            "/",
            current.container_id,
            *argv,
        ]
        return self._run(command, timeout=timeout_s)

    def probe_guest_observer(self) -> str | None:
        """Prove the guest kernel, retained exec isolation and real eBPF delivery.

        The observation stages are retried because they race a guest that is
        still starting. Everything before them is a property of the image and
        the kernel: it answers the same way every time, so it is raised on the
        first attempt rather than being asked three times on a host that is
        genuinely missing a capability.

        A rollout hangs on the answer. `cmd_up` invalidates this host's
        shared-live marker and drains the runners that were serving before it
        runs this, and completes the marker only once every replica passes, so
        a probe that fails leaves the host advertising no deployment at all --
        and a runner that advertises no deployment does not poll. One lost race
        in a freshly booted microVM took staging off the air for 29.6 hours on
        2026-09-13, because nothing retried and nothing else deployed until the
        next day.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return None
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._probe_guest_observer_once()
            except _ObserverProbeRace as exc:
                if attempts >= GUEST_OBSERVER_PROBE_ATTEMPTS:
                    raise SandboxError(
                        f"{exc} (after {attempts} attempts on a fresh sandbox each time)"
                    ) from exc
                logger.warning(
                    "Kata observer probe lost a race with its own guest; retrying: %s",
                    exc,
                    extra={"event": "guest_observer_probe_retry", "attempt": attempts},
                )
                time.sleep(GUEST_OBSERVER_PROBE_RETRY_S)

    def _remove_abandoned_probes(self) -> None:
        """Remove this namespace's probes left behind by a process killed mid-probe.

        Best effort: a listing that fails leaves them for the next probe, and
        the probe about to run reports a Docker that is genuinely unusable.
        """
        listed = self._run(
            [
                self.backend.docker_binary,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"label={CHAT_PROBE_LABEL}",
                "--format",
                '{"id":{{json .ID}},"namespace":{{json (.Label "'
                + CHAT_NAMESPACE_LABEL
                + '")}},"created":{{json (.Label "'
                + CHAT_PROBE_LABEL
                + '")}}}',
            ],
            timeout=30,
        )
        if listed.returncode != 0:
            return
        abandoned_before = time.time() - PROBE_ABANDONED_AFTER_S
        for line in listed.stdout.splitlines():
            try:
                row = json.loads(line)
                container_id = row["id"]
                if (
                    not isinstance(container_id, str)
                    or not container_id
                    or row["namespace"] != self.backend.sandbox_namespace
                    or int(row["created"]) > abandoned_before
                ):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            self._run([self.backend.docker_binary, "rm", "--force", container_id], timeout=30)

    def _probe_guest_observer_once(self) -> str:
        """One probe, on a sandbox of its own that is removed however it ends."""
        self._remove_abandoned_probes()
        token = secrets.token_hex(12)
        spec = ChatSandboxSpec(
            thread_id=f"athr_preflight_{token}",
            sandbox_id=f"asbx_preflight_{token}",
            generation=1,
            runtime_id="preflight",
            resident_slot_capacity=2,
            labels={CHAT_PROBE_LABEL: str(int(time.time()))},
        )
        command = self.build_create_command(spec)
        # Use the production creation/exec flags, without attaching any relay
        # or touching retained user state. No repository or credential enters.
        command[command.index("--network") + 1] = "none"
        created = self._run(command, timeout=120)
        if created.returncode != 0:
            raise SandboxError("Kata observer probe creation failed: " + created.stderr[:300])
        try:
            started = self._run(
                [self.backend.docker_binary, "start", spec.container_name], timeout=120
            )
            if started.returncode != 0:
                raise SandboxError("Kata observer probe start failed: " + started.stderr[:300])
            isolation = self.exec_capture(
                spec,
                [
                    "python3",
                    "-c",
                    "import json, os; "
                    "s=dict(line.split(':',1) for line in open('/proc/self/status') "
                    "if ':' in line); "
                    "print(json.dumps(dict(kernel=os.uname().release, uid=os.getuid(), "
                    "nnp=int(s['NoNewPrivs']), seccomp=int(s['Seccomp']), "
                    "effective=int(s['CapEff'],16), permitted=int(s['CapPrm'],16))))",
                ],
                workdir="/",
                timeout_s=30,
            )
            try:
                observed = json.loads(isolation.stdout)
            except (ValueError, TypeError) as exc:
                raise SandboxError(
                    "Kata observer probe could not inspect member isolation"
                ) from exc
            if not isinstance(observed, dict):
                raise SandboxError("Kata observer probe could not inspect member isolation")
            guest_kernel = self.backend.validate_guest_kernel(observed.pop("kernel", None))
            # validate_guest_kernel refuses a shared kernel for Kata; keep
            # that requirement explicit before any guest-root helper runs.
            if guest_kernel is None:
                raise SandboxError("Kata observer probe returned no guest kernel")
            if (
                isolation.returncode != 0
                or observed
                != {
                    "uid": self.backend.uid,
                    "nnp": 1,
                    "seccomp": 2,
                    "effective": 0,
                    "permitted": 0,
                }
                or self.backend.uid == 0
            ):
                raise SandboxError("Kata member exec lacks non-root, seccomp or no-new-privileges")
            cleanup = self.exec_capture(
                spec,
                [MEMBER_REAPER_TARGET, "reap", str(MEMBER_UID_BASE)],
                workdir="/",
                input_bytes=b"",
                user_uid=MEMBER_UID_BASE,
                user_gid=self.backend.gid,
                timeout_s=30,
            )
            if cleanup.returncode != 0:
                raise SandboxError(
                    "Kata member process cleanup self-test failed: "
                    + cleanup.stderr.decode("utf-8", "replace")[:300]
                )
            observer = self._kata_privileged_exec(
                spec, [GUEST_OBSERVER_BINARY, "--self-test"], timeout_s=30
            )
            if observer.returncode != 0:
                raise SandboxError("Kata guest observer self-test failed: " + observer.stderr[:500])
            self._check_observer_self_test(observer.stdout)
            marker = self.exec_capture(
                spec,
                ["/usr/local/libexec/freeinference-tool-mark", "--self-test"],
                workdir="/",
                timeout_s=30,
            )
            if marker.returncode != 0 or marker.stdout.strip() != b"freeinference-tool-mark-v1":
                raise SandboxError("Kata tool marker self-test failed")
            cursor = self.begin_guest_observation(spec, user_uid=self.backend.uid)
            if not cursor.available:
                # `begin` folds every reason into an unavailable cursor, so
                # without its detail this reads the same whether the observer
                # died, answered a version this host rejects, or had simply not
                # published its first status yet.
                raise _ObserverProbeRace(
                    "Kata tool marker transport observer is unavailable: "
                    + (cursor.detail or "no detail reported")
                )
            for phase in ("pre", "post"):
                sent = self.exec_capture(
                    spec,
                    [
                        "/usr/local/libexec/freeinference-tool-mark",
                        phase,
                        "/agent-state/tool-marks.log",
                    ],
                    input_bytes=b'{"tool_use_id":"preflight"}',
                    workdir="/",
                    timeout_s=30,
                )
                if sent.returncode != 0:
                    raise SandboxError("Kata tool marker transport helper failed")
            observation = self.finish_guest_observation(spec, cursor)
            delivered = [(mark.phase, mark.tool_use_id) for mark in observation.tool_marks]
            if not observation.complete or delivered != [
                ("pre", "preflight"),
                ("post", "preflight"),
            ]:
                # Two different failures wore one sentence here, which is why
                # the 2026-09-13 rollout could not be diagnosed from its logs:
                # an incomplete slice means the journal could not be read back,
                # while a complete slice with the wrong marks means the
                # boundaries themselves never arrived.
                raise _ObserverProbeRace(
                    "Kata observer did not receive trusted tool boundaries: "
                    f"complete={observation.complete} "
                    f"lost_events={observation.lost_events} "
                    f"truncated={observation.truncated} "
                    f"marks={delivered!r} "
                    + (f"detail={observation.detail}" if observation.detail else "")
                )
            return guest_kernel
        finally:
            self._run(
                [self.backend.docker_binary, "rm", "--force", spec.container_name], timeout=30
            )

    def _check_observer_self_test(self, stdout: str) -> None:
        """Accept the helper's identity line and report its window matrix.

        The helper answers with ``freeinference-guest-observer-v<N>`` and, from
        version 2, a second line naming the live counters this guest can
        actually read. A capability this kernel lacks is reported here, where
        an operator reads preflight output, rather than being discovered as a
        permanently partial window later. It is deliberately not fatal: a
        guest without a live census still observes process lifecycles, and
        the platform offers what it can prove rather than refusing the host.
        """
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("freeinference-guest-observer-v"):
            raise SandboxError("Kata guest observer self-test failed: unrecognised helper identity")
        if len(lines) < 2:
            logger.warning(
                "Kata guest observer reports no member window capability",
                extra={"event": "guest_observer_window_capability_absent"},
            )
            return
        try:
            capability = json.loads(lines[1]).get("window") or {}
        except (ValueError, AttributeError):
            logger.warning(
                "Kata guest observer capability report is unreadable",
                extra={"event": "guest_observer_window_capability_unreadable"},
            )
            return
        missing = [name for name in ("cpu", "storage_io", "pss") if not capability.get(name)]
        if missing:
            logger.warning(
                "Kata guest cannot measure part of a member window: %s",
                ", ".join(missing),
                extra={
                    "event": "guest_observer_window_capability_partial",
                    "missing": missing,
                },
            )

    def _observer_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        detach: bool = False,
        timeout_s: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Keep the observer-only seam explicit for tests and call sites."""
        return self._kata_privileged_exec(
            spec,
            argv,
            detach=detach,
            timeout_s=timeout_s,
        )

    def _guest_observer_snapshot(
        self,
        spec: ChatSandboxSpec,
        *,
        after_poll_seq: int | None = None,
    ) -> GuestObserverSnapshot:
        """Read an atomic status/size/guest-clock tuple from the root journal."""
        try:
            return self._guest_observer_controller(spec).snapshot(after_poll_seq=after_poll_seq)
        except GuestObserverRuntimeError as exc:
            raise SandboxError(str(exc)) from exc

    def member_window_journal_delta(
        self,
        spec: ChatSandboxSpec,
        cursor: GuestObserverCursor,
        *,
        timeout_s: float = 10.0,
    ) -> tuple[bytes, int]:
        """Read the journal bytes this window has not consumed yet.

        Returns the payload and the offset it ended at, so a caller can keep
        one reducer per window and fold each cycle in exactly once. The read is
        fenced exactly like the observation read is: the device, inode and size
        must all still describe the same journal, because a restarted observer
        would otherwise hand back bytes from a different file under the same
        path and silently restart the counters.
        """
        if not cursor.available or not cursor.journal_ino:
            return b"", cursor.offset
        reader = (
            "import os, sys\n"
            "filename, offset, expected_dev, expected_ino = sys.argv[1], int(sys.argv[2]), "
            "int(sys.argv[3]), int(sys.argv[4])\n"
            "with open(filename, 'rb', buffering=0) as handle:\n"
            "    journal = os.fstat(handle.fileno())\n"
            "    if (int(journal.st_dev) != expected_dev "
            "            or int(journal.st_ino) != expected_ino):\n"
            "        raise RuntimeError('observer journal changed before read')\n"
            "    if int(journal.st_size) < offset:\n"
            "        raise RuntimeError('observer journal is behind the read fence')\n"
            "    handle.seek(offset)\n"
            "    sys.stdout.buffer.write(handle.read(max(0, int(journal.st_size) - offset)))\n"
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                reader,
                GUEST_OBSERVER_EVENTS,
                str(cursor.offset),
                str(cursor.journal_dev),
                str(cursor.journal_ino),
            ],
            workdir="/",
            timeout_s=timeout_s,
            user_uid=0,
            user_gid=0,
        )
        if getattr(result, "returncode", 1) != 0:
            detail = getattr(result, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise SandboxError(f"member window journal read failed: {str(detail)[-300:]}")
        payload = getattr(result, "stdout", b"")
        if not isinstance(payload, bytes):
            payload = str(payload).encode("utf-8", "replace")
        return payload, cursor.offset + len(payload)

    def guest_boot_id(self, spec: ChatSandboxSpec) -> str:
        """This guest's boot identifier, or an empty string when unreadable.

        A task's identity is only unique within one boot, so every instance key
        carries it. The observer controller caches the read, so asking once per
        window costs one guest exec rather than one per census.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return ""
        try:
            return self._guest_observer_controller(spec).guest_boot_id()
        except Exception:
            return ""

    def member_window_request(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        timeout_s: float,
    ) -> subprocess.CompletedProcess[bytes]:
        """Ask the guest observer for one member execution window operation.

        The window helper is a second reviewed binary beside the tool marker,
        and the observer accepts a window request only from it. Nothing here
        composes a command from job input: ``argv`` comes from the fixed
        ``sample``/``register``/``close`` verbs inside
        :class:`~cloud_agent_host.member_resources.MemberWindowSampler`, and
        the container is the same label-verified sandbox the turn is already
        running in.
        """
        return self.exec_capture(
            spec,
            argv,
            workdir="/",
            timeout_s=timeout_s,
            user_uid=0,
            user_gid=0,
        )

    def _guest_observer_controller(self, spec: ChatSandboxSpec) -> GuestObserverController:
        """Bind the shared observer contract to this label-verified container."""

        def capture(
            argv: builtins.list[str], timeout_s: float
        ) -> subprocess.CompletedProcess[bytes]:
            return self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=timeout_s,
                user_uid=0,
                user_gid=0,
            )

        def privileged(
            argv: builtins.list[str], detach: bool, timeout_s: float
        ) -> subprocess.CompletedProcess[str]:
            return self._observer_privileged_exec(
                spec,
                argv,
                detach=detach,
                timeout_s=timeout_s,
            )

        return GuestObserverController(
            capture=capture,
            privileged=privileged,
            lock=self._guest_observer_lock,
        )

    def ensure_guest_observer(self, spec: ChatSandboxSpec) -> None:
        """Start the reviewed eBPF helper and wait until all probes are attached."""
        if self.backend.runtime != KATA_RUNTIME:
            raise SandboxError("guest process observation requires the Kata runtime")
        try:
            self._guest_observer_controller(spec).ensure()
        except GuestObserverRuntimeError as exc:
            raise SandboxError(str(exc)) from exc

    def _start_guest_observer(self, spec: ChatSandboxSpec) -> None:
        """Replace a stale singleton; caller holds ``_guest_observer_lock``."""
        try:
            self._guest_observer_controller(spec).start()
        except GuestObserverRuntimeError as exc:
            raise SandboxError(str(exc)) from exc

    def begin_guest_observation(
        self,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        measurement_id: str | None = None,
    ) -> GuestObserverCursor:
        """Fence the journal immediately before one member prompt is admitted.

        ``measurement_id`` additionally marks this fence as the start of a
        member execution window, so the observation also carries that window's
        live census cycles. The fence has to be taken before the window's
        opening census, which is why the identifier travels with it here.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return GuestObserverCursor.unavailable(user_uid, "sandbox is not a Kata guest")
        controller = self._guest_observer_controller(spec)
        key = spec.container_name
        with self._guest_observer_lock:
            active = self._guest_observer_active.setdefault(key, {})
            if not active:
                controller.rotate_if_needed(threshold=GUEST_OBSERVER_ROTATE_BYTES)
            cursor = controller.begin(
                user_uid=user_uid,
                # Never sacrifice concurrent member observations to admit one
                # more turn. If the singleton is temporarily unreadable, this
                # cursor is explicitly unavailable; cleanup of existing owners
                # makes a later idle begin eligible to restart it.
                restart_if_missing=not active,
                measurement_id=measurement_id,
            )
            if cursor.available:
                # A restart invalidates every old-pid cursor already; do not
                # let abandoned bookkeeping suppress future safe rotations.
                active = {
                    token: observer_pid
                    for token, observer_pid in active.items()
                    if observer_pid == cursor.observer_pid
                }
                active[id(cursor)] = cursor.observer_pid
                self._guest_observer_active[key] = active
            elif not active:
                self._guest_observer_active.pop(key, None)
            return cursor

    def finish_guest_observation(
        self,
        spec: ChatSandboxSpec,
        cursor: GuestObserverCursor | None,
    ) -> GuestObservation:
        """Drain and read exactly one cursor's bounded member event slice."""
        try:
            return self._guest_observer_controller(spec).finish(cursor)
        finally:
            if cursor is not None:
                key = spec.container_name
                with self._guest_observer_lock:
                    active = self._guest_observer_active.get(key)
                    if active is not None:
                        active.pop(id(cursor), None)
                        if not active:
                            self._guest_observer_active.pop(key, None)

    def tool_mark_reader(self, spec: ChatSandboxSpec) -> ToolMarkReader:
        """Return a bounded reader for hook marks inside one retained container."""
        if self.backend.runtime == KATA_RUNTIME:
            # Kata accepts marks only through the guest-root observer socket.
            # The agent-owned compatibility file is deliberately not an
            # attribution authority in an untrusted sandbox.
            return ToolMarkReader(None)
        path = f"{CHAT_STATE_DIR}/{TOOL_MARK_FILE}"

        def remote_size() -> int:
            script = (
                "import os, sys\n"
                "try:\n"
                "    size = os.path.getsize(sys.argv[1])\n"
                "except OSError:\n"
                "    size = 0\n"
                "print(size)\n"
            )
            result = self.exec_capture(
                spec,
                ["python3", "-c", script, path],
                workdir="/",
                timeout_s=5,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0:
                raise SandboxError("tool-mark fence could not be read")
            return int(result.stdout.decode("ascii", "strict").strip() or "0")

        def remote_read(offset: int, count: int) -> bytes:
            bounded = min(max(0, count), MAX_TOOL_MARK_BYTES)
            script = (
                "import os, sys\n"
                "path, offset, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                "try:\n"
                "    with open(path, 'rb', buffering=0) as handle:\n"
                "        handle.seek(offset)\n"
                "        sys.stdout.buffer.write(handle.read(count))\n"
                "except FileNotFoundError:\n"
                "    pass\n"
            )
            result = self.exec_capture(
                spec,
                ["python3", "-c", script, path, str(max(0, offset)), str(bounded)],
                workdir="/",
                timeout_s=5,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0 or len(result.stdout) > bounded:
                raise SandboxError("tool-mark slice could not be read")
            return result.stdout

        return ToolMarkReader(
            None,
            remote_size=remote_size,
            remote_read=remote_read,
        )

    def install_tool_hooks(self, spec: ChatSandboxSpec, update: dict[str, Any]) -> None:
        """Atomically merge trusted Claude hook settings inside the container.

        Docker-owned retained state has no corresponding host path. Writing it
        with :mod:`pathlib` on the broker would configure the broker itself,
        not the Agent CLI. A group-writable compatibility file supports trusted
        non-Kata development. Kata ignores that file and accepts marks only in
        the guest-root observer journal.
        """
        payload = json.dumps(update, separators=(",", ":")).encode()
        if len(payload) > 64 * 1024:
            raise SandboxError("tool hook settings exceed their trusted bound")
        script = (
            "import contextlib, fcntl, json, os, pathlib, sys\n"
            "config = pathlib.Path(sys.argv[1])\n"
            "marks = pathlib.Path(sys.argv[2])\n"
            "raw = sys.stdin.buffer.read(65537)\n"
            "if len(raw) > 65536:\n"
            "    raise RuntimeError('tool hook settings are too large')\n"
            "update = json.loads(raw)\n"
            "if not isinstance(update, dict):\n"
            "    raise RuntimeError('tool hook settings are not an object')\n"
            "os.umask(0o007)\n"
            "config.mkdir(parents=True, exist_ok=True)\n"
            "marks.parent.mkdir(parents=True, exist_ok=True)\n"
            "mark_fd = os.open(marks, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o660)\n"
            "os.close(mark_fd)\n"
            "with contextlib.suppress(PermissionError):\n"
            "    os.chmod(marks, 0o660)\n"
            "lock_path = config / '.freeinference-hooks.lock'\n"
            "lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o660)\n"
            "with os.fdopen(lock_fd, 'r+') as lock:\n"
            "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "    settings_path = config / 'settings.json'\n"
            "    settings = {}\n"
            "    try:\n"
            "        candidate = json.loads(settings_path.read_text())\n"
            "        if isinstance(candidate, dict):\n"
            "            settings = candidate\n"
            "    except (OSError, ValueError, TypeError):\n"
            "        pass\n"
            "    settings.update(update)\n"
            "    temporary = config / f'.settings.{os.getpid()}.tmp'\n"
            "    try:\n"
            "        temporary.write_text(json.dumps(settings, indent=2))\n"
            "        os.chmod(temporary, 0o660)\n"
            "        os.replace(temporary, settings_path)\n"
            "    finally:\n"
            "        with contextlib.suppress(FileNotFoundError):\n"
            "            temporary.unlink()\n"
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                f"{CHAT_STATE_DIR}/claude",
                f"{CHAT_STATE_DIR}/{TOOL_MARK_FILE}",
            ],
            workdir="/",
            timeout_s=10,
            input_bytes=payload,
        )
        if result.returncode != 0:
            raise SandboxError(
                "tool hooks could not be installed in retained state: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def reset_unsettled_state(self, spec: ChatSandboxSpec) -> None:
        """Clear a never-settled first-turn import before an idempotent retry.

        This is deliberately unavailable as a generic path-taking primitive:
        only the two Docker-layer roots owned by this exact, label-verified
        sandbox are cleared. A failed prompt admission is marked LOST and can
        never reach this method; it is only for checkout/setup failures before
        the Agent CLI saw the user's message.
        """
        script = (
            "set -eu; "
            f"for root in {CHAT_WORKDIR} {CHAT_STATE_DIR}; do "
            'find "$root" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; '
            "done"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script],
            workdir="/",
            timeout_s=120,
        )
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox unsettled-state reset failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def import_base_bundle(
        self,
        spec: ChatSandboxSpec,
        bundle: bytes,
        *,
        commit: str,
        env: dict[str, str] | None = None,
    ) -> None:
        """Import publisher-base objects without changing workspace Git state.

        The bundle crosses stdin into activation tmpfs and is removed before
        this command returns. ``git bundle unbundle`` adds objects only: it
        does not move HEAD, refs, the real index, or working-tree files.
        """
        if not bundle:
            raise ValueError("base bundle must be non-empty")
        if not _FULL_OBJECT_ID.fullmatch(commit):
            raise ValueError("base bundle commit must be a full Git object id")
        script = (
            'set -eu; expected="$1"; object="$2"; '
            f"bundle={ACTIVATION_TMPFS}/base.bundle; "
            "umask 077; trap 'rm -f \"$bundle\"' EXIT HUP INT TERM; "
            'cat > "$bundle"; '
            'set -- $(git bundle list-heads "$bundle" HEAD); '
            'test "$#" -eq 2; test "$1" = "$expected"; test "$2" = HEAD; '
            'git bundle verify "$bundle" >/dev/null; '
            'git bundle unbundle "$bundle" >/dev/null; '
            'git cat-file -e "$object"'
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", commit, f"{commit}^{{commit}}"],
            env=env,
            workdir=CHAT_WORKDIR,
            timeout_s=300,
            input_bytes=bundle,
        )
        if result.returncode != 0:
            raise SandboxError(
                "retained base bundle import failed: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def export_archive(self, spec: ChatSandboxSpec, path: str) -> bytes:
        """Read one path through Docker's archive boundary (``docker cp``)."""
        self._require(spec)
        self._validate_container_path(path)
        result = self._run_bytes(
            [
                self.backend.docker_binary,
                "cp",
                f"{spec.container_name}:{path}",
                "-",
            ],
            timeout=120,
        )
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox archive export failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )
        return result.stdout

    def import_archive(self, spec: ChatSandboxSpec, path: str, archive: bytes | BinaryIO) -> None:
        """Write a validated caller-provided tar through Docker's archive boundary.

        ``archive`` is the tar itself or a real file positioned at its start. A
        file becomes ``docker cp``'s standard input, so a large checkout is never
        read into this process.
        """
        self._require(spec)
        self._validate_container_path(path)
        if isinstance(archive, bytes):
            size, input_bytes, stdin = len(archive), archive, None
        else:
            size = os.fstat(archive.fileno()).st_size - archive.tell()
            input_bytes, stdin = None, archive
        result = self._run_bytes(
            [
                self.backend.docker_binary,
                "cp",
                "-",
                f"{spec.container_name}:{path}",
            ],
            timeout=ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S * max(1.0, size / (1 << 30)),
            input_bytes=input_bytes,
            stdin=stdin,
        )
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox archive import failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def _require(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        current = self.inspect(spec.container_name)
        if current is None:
            raise SandboxError("chat sandbox is missing")
        self._verify_identity(current, spec)
        return current

    @staticmethod
    def _status(state: dict[str, Any]) -> ChatSandboxStatus:
        if bool(state.get("Paused")):
            return ChatSandboxStatus.PAUSED
        raw = str(state.get("Status") or "").lower()
        if raw == "exited":
            return ChatSandboxStatus.STOPPED
        try:
            return ChatSandboxStatus(raw)
        except ValueError:
            return ChatSandboxStatus.UNKNOWN

    @staticmethod
    def _verify_mounts(inspection: ChatSandboxInspection) -> None:
        """Chat state must be in the writable layer, never a host/volume mount."""
        for mount in inspection.mounts:
            mount_type = str(mount.get("Type") or "")
            destination = str(mount.get("Destination") or "")
            if mount_type in {"bind", "volume"}:
                raise SandboxError(
                    f"retained chat sandbox has forbidden {mount_type} mount at "
                    f"{destination or '<unknown>'}"
                )
            if destination in {CHAT_WORKDIR, CHAT_STATE_DIR}:
                raise SandboxError(
                    f"retained chat state path {destination!r} is mounted, not Docker-layer owned"
                )

    def _verify_identity(self, inspection: ChatSandboxInspection, spec: ChatSandboxSpec) -> None:
        expected = spec.docker_labels()
        mismatched = {
            key: (inspection.labels.get(key), value)
            for key, value in expected.items()
            if inspection.labels.get(key) != value
        }
        namespace = inspection.labels.get(CHAT_NAMESPACE_LABEL)
        if namespace is not None and namespace != self.backend.sandbox_namespace:
            mismatched[CHAT_NAMESPACE_LABEL] = (
                namespace,
                self.backend.sandbox_namespace,
            )
        if spec.sandbox_image_digest is not None and inspection.image != spec.sandbox_image_digest:
            mismatched["container-image"] = (
                inspection.image,
                spec.sandbox_image_digest,
            )
        if inspection.name != spec.container_name or mismatched:
            raise SandboxError(
                "Docker name is occupied by a container outside this chat/generation"
            )

    def _validate_extra_args(self) -> None:
        """Reject options that would defeat retained ownership or secret rules."""
        refused = {
            "--rm",
            "--name",
            "--label",
            "--mount",
            "--volume",
            "-v",
            "--volumes-from",
            "--env",
            "-e",
            "--env-file",
            "--entrypoint",
            "--user",
            "--network",
            "--privileged",
            "--cap-add",
            "--device",
            "--pid",
            "--ipc",
            "--uts",
        }
        for token in self.backend.extra_args:
            option = token.split("=", 1)[0]
            if option in refused:
                raise SandboxError(
                    f"AGENT_SANDBOX_EXTRA_ARGS option {option!r} is unsafe for retained chats"
                )

    @staticmethod
    def _validate_container_path(path: str) -> None:
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("container archive path must be absolute")

    @staticmethod
    def _run(argv: builtins.list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                env={**os.environ},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Docker lifecycle command failed: {exc}") from exc

    @staticmethod
    def _run_bytes(
        argv: builtins.list[str],
        *,
        timeout: float,
        input_bytes: bytes | None = None,
        stdin: BinaryIO | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv,
                env={**os.environ},
                input=input_bytes,
                stdin=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Docker archive command failed: {exc}") from exc


class _RetainedExecProcess(SandboxProcess):
    """An attached ``docker exec`` with container- or member-scoped cleanup."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        process_pidfile: str | None,
        user_uid: int | None,
        user_gid: int | None,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
        self._process_pidfile = process_pidfile
        self._user_uid = user_uid
        self._user_gid = user_gid
        self._stderr_chunks: builtins.list[str] = []
        self._stderr_thread: threading.Thread | None = None
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                daemon=True,
                name="retained-sandbox-stderr",
            )
            self._stderr_thread.start()

    @property
    def container(self) -> str:
        return self._spec.container_name

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr_chunks.append(line)
            del self._stderr_chunks[:-200]

    def lines(self) -> Iterator[str]:
        assert self._process.stdout is not None
        yield from self._process.stdout

    def write_line(self, text: str) -> None:
        if self._process.stdin is None:
            raise SandboxError("this retained exec was not spawned with live input")
        self._process.stdin.write(text.rstrip("\n") + "\n")
        self._process.stdin.flush()

    def close_stdin(self) -> None:
        if self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()

    def poll(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        """Kill one shared member, stopping the container only as a backstop."""
        try:
            if self._process_pidfile is not None:
                assert self._user_uid is not None and self._user_gid is not None
                self._manager.kill_member_process(
                    self._spec,
                    pidfile=self._process_pidfile,
                    user_uid=self._user_uid,
                    user_gid=self._user_gid,
                )
            else:
                self._manager.stop(self._spec)
        finally:
            if self._process.poll() is None:
                self._process.kill()

    def wait(self) -> int:
        return self._process.wait()

    def stderr_text(self) -> str:
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        return "".join(self._stderr_chunks)


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class _RetainedTerminalProcess(TerminalProcess):
    """One Docker-exec PTY whose lifecycle never removes the chat container."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        pidfile: str,
        input_fd: int,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
        self._pidfile = pidfile
        self._input_fd: int | None = input_fd
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._killed = False
        self._suspended = False

    def chunks(self) -> Iterator[bytes]:
        assert self._process.stdout is not None
        while chunk := self._process.stdout.read(64 * 1024):
            yield chunk

    def write(self, data: bytes) -> None:
        with self._write_lock:
            if self._process.poll() is not None or self._input_fd is None:
                raise SandboxError("terminal is closed")
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(self._input_fd, remaining)
                    if written == 0:
                        raise OSError("terminal input closed")
                    remaining = remaining[written:]
            except OSError as exc:
                raise SandboxError("terminal is closed") from exc

    def resize(self, rows: int, cols: int) -> None:
        with self._write_lock:
            if self._input_fd is None or self._process.poll() is not None:
                raise SandboxError("terminal is closed")
            try:
                _set_pty_size(self._input_fd, rows, cols)
                # Docker CLI watches its controlling TTY on SIGWINCH and
                # forwards the new dimensions to this exec instance.
                os.kill(self._process.pid, signal.SIGWINCH)
            except OSError as exc:
                raise SandboxError("terminal cannot be resized") from exc

    def _signal_tree(self, signal_name: str) -> None:
        script = (
            'attempt=0; while [ ! -r "$1" ]; do '
            'attempt=$((attempt + 1)); test "$attempt" -lt 100 || exit 3; '
            'sleep 0.05; done; pid=$(cat "$1") || exit 4; '
            'case "$pid" in ""|*[!0-9]*) exit 2;; esac; '
            # The minimal sandbox image intentionally has no /bin/kill. The
            # POSIX shell builtin supports negative process-group operands.
            f'kill -{signal_name} -"$pid" 2>/dev/null'
        )
        result = self._manager.exec_capture(
            self._spec,
            ["/bin/sh", "-c", script, "sh", self._pidfile],
            timeout_s=15,
        )
        # Natural exit can win the small poll-to-signal race. That is already
        # the requested end state; a live client without a confirmed signal
        # must fail closed so protected workspace phases cannot overlap it.
        if result.returncode != 0 and self._process.poll() is None:
            raise SandboxError(f"terminal cannot receive {signal_name}")

    def _set_suspended(self, suspended: bool) -> None:
        with self._state_lock, self._write_lock:
            if self._killed or self._process.poll() is not None:
                return
            if self._suspended is suspended:
                return
            self._signal_tree("STOP" if suspended else "CONT")
            self._suspended = suspended

    def suspend(self) -> None:
        self._set_suspended(True)

    def resume(self) -> None:
        self._set_suspended(False)

    def _close_input(self) -> None:
        with self._write_lock:
            input_fd = self._input_fd
            self._input_fd = None
            if input_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(input_fd)

    def kill(self) -> None:
        with self._state_lock:
            if self._killed:
                return
            if self._process.poll() is None:
                self._signal_tree("TERM")
                deadline = time.monotonic() + 3
                while self._process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
            if self._process.poll() is None:
                self._signal_tree("KILL")
            if self._process.poll() is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(self._process.pid, signal.SIGKILL)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise SandboxError("terminal Docker exec could not be stopped") from exc
            self._close_input()
            self._killed = True

    def wait(self) -> int:
        try:
            return self._process.wait()
        finally:
            self._close_input()


#: How long a turn's commands rely on a proof that the group bridge is in
#: place before checking again: the minute the host guard already trusts its
#: own last verification for.
TURN_NETWORK_PROOF_S = GUARD_RECHECK_S


class RetainedChatBackend(SandboxBackend):
    """Present one retained container through the runner's sandbox contract.

    This adapter is what lets runtime parsing, setup, patch building and Agent
    CLI drivers execute with ``docker exec``. It never interprets a host
    ``workdir`` or ``state_dir``: both canonical paths already live in the
    container writable layer.
    """

    name = "retained-container"

    def __init__(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        *,
        network_proved_at: float | None = None,
    ) -> None:
        self.manager = manager
        self.spec = spec
        self.image = spec.sandbox_image_digest or manager.backend.image
        self.runtime = manager.backend.runtime
        # Monotonic time the group bridge was last proved, for a turn's backend
        # only; ``None`` checks before every command.
        self._network_proved_at = network_proved_at

    @property
    def provides_isolation(self) -> bool:
        """Report the retained container as the outer execution boundary."""
        return True

    @property
    def is_vm_isolated(self) -> bool:
        """Mirror whether the underlying container runtime uses a guest VM."""
        return self.manager.backend.is_vm_isolated

    def sandbox_metadata(self) -> dict[str, str]:
        """Describe the retained lifecycle alongside backend isolation metadata."""
        return {
            **self.manager.backend.sandbox_metadata(),
            "sandbox_lifecycle": "restartable",
            "sandbox_id": self.spec.sandbox_id,
        }

    def version_stamp(self) -> dict[str, Any]:
        """Return a stamp that describes the retained image, not today's image."""
        stamp = self.manager.backend.version_stamp()
        if self.spec.sandbox_image_digest:
            stamp = {
                **stamp,
                "sandbox_image_digest": self.spec.sandbox_image_digest,
            }
        if self.spec.runtime_version:
            versions = dict(stamp.get("runtime_versions") or {})
            versions[self.spec.runtime_id] = self.spec.runtime_version
            available = dict(stamp.get("runtime_versions_available") or {})
            available[self.spec.runtime_id] = [self.spec.runtime_version]
            stamp = {
                **stamp,
                "runtime_versions": versions,
                "runtime_versions_available": available,
            }
        return stamp

    def base_env(self) -> dict[str, str]:
        """Return the underlying backend's hermetic sandbox environment."""
        return self.manager.backend.base_env()

    def has_binary(self, name: str) -> bool:
        """Check for one binary inside the exact retained container."""
        if not name or "\x00" in name:
            return False
        result = self.manager.exec_capture(
            self.spec,
            ["/bin/sh", "-c", 'command -v -- "$1" >/dev/null 2>&1', "sh", name],
            timeout_s=30,
        )
        return result.returncode == 0

    def require_network(self, *, observer: StageObserver | None = None) -> None:
        """Prove the container is on its guarded group bridge before running anything.

        A turn's backend starts from the proof its activation made: the broker
        checked exactly this when it acquired the turn, moments before. That
        proof, and each one this backend makes itself, stands for
        :data:`TURN_NETWORK_PROOF_S`; a command after that, such as one after a
        long setup script or at the end of a long turn, checks again. A backend
        built without a proof checks before every command.
        """
        proved_at = self._network_proved_at
        if proved_at is not None and time.monotonic() - proved_at < TURN_NETWORK_PROOF_S:
            return
        started = time.monotonic()
        self.manager.require_network(self.spec, observer=observer)
        if proved_at is not None:
            self._network_proved_at = started

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Execute one Agent CLI or bounded command in the retained container."""
        self.require_network()
        return self.manager.spawn(
            self.spec,
            spec.argv,
            env=dict(spec.env),
            workdir=CHAT_WORKDIR,
            live_input=spec.live_input,
            user_uid=self.manager.backend.uid,
            user_gid=self.manager.backend.gid,
            process_pidfile=None,
        )

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        """Open an interactive PTY in this chat's retained workspace."""
        if spec.workdir != CHAT_WORKDIR:
            raise SandboxError("retained terminal must run in the chat workspace")
        return self.manager.spawn_terminal(
            self.spec,
            env=spec.env,
            workdir=CHAT_WORKDIR,
            rows=rows,
            cols=cols,
        )

    def preflight(self) -> None:
        """Use the container backend checks without probing a host bind mount."""
        self.manager.backend.preflight()

    def check_gateway_reachable(self, base_url: str) -> None:
        """Delegate the backend's isolated gateway reachability probe."""
        self.manager.backend.check_gateway_reachable(base_url)


__all__ = [
    "ACTIVATION_TMPFS",
    "CHAT_CONTAINER_PREFIX",
    "CHAT_GENERATION_LABEL",
    "CHAT_IMAGE_DIGEST_LABEL",
    "CHAT_NAMESPACE_LABEL",
    "CHAT_RESIDENT_CAPACITY_LABEL",
    "CHAT_RUNTIME_LABEL",
    "CHAT_RUNTIME_VERSION_LABEL",
    "CHAT_SANDBOX_ID_LABEL",
    "CHAT_SANDBOX_LABEL",
    "CHAT_SHARED_LABEL",
    "CHAT_STATE_DIR",
    "CHAT_THREAD_LABEL",
    "CHAT_UPLOADS_DIR",
    "CHAT_WORKDIR",
    "MEMBER_TMPFS_ROOT",
    "MEMBER_UID_BASE",
    "MEMBER_UID_LIMIT",
    "TURN_NETWORK_PROOF_S",
    "ChatSandboxInspection",
    "ChatSandboxManager",
    "ChatSandboxSpec",
    "ChatSandboxStatus",
    "RetainedChatBackend",
    "RetainedChatLost",
]
