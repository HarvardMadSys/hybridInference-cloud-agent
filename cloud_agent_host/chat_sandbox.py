"""Docker-owned, restartable sandboxes for agent chats.

This module is the storage/lifecycle boundary for the restartable execution
mode.  A chat sandbox is deliberately different from :class:`SandboxBackend`'s
legacy ``docker run --rm`` process:

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
import json
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
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_ROOT,
    GUEST_OBSERVER_ROTATE_BYTES,
    GuestObservation,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverRuntimeError,
)
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

if TYPE_CHECKING:
    from collections.abc import Iterator


CHAT_SANDBOX_LABEL = "org.freeinference.chat-sandbox"
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
CHAT_CONTAINER_PREFIX = "freeinference-chat"
CHAT_WORKDIR = "/workspace"
CHAT_STATE_DIR = "/agent-state"
ACTIVATION_TMPFS = "/run/agent-activation"
MEMBER_TMPFS_ROOT = "/run/agent-members"
MEMBER_UID_BASE = 11001
MEMBER_UID_LIMIT = 128
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")

# PID 1 contains no chat credential and owns no chat state outside the
# container writable layer. A reconnectable protocol bridge may later replace
# this inert process as a latency optimization; continuity does not depend on
# it. Docker's ``--init`` owns signal forwarding/reaping, while one never-
# respawned child avoids manufacturing a same-UID process event every hour.
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
    shared_live: bool = False
    resident_slot_capacity: int = 8
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
        if self.shared_live:
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


class ChatSandboxManager:
    """Create and operate retained chat containers through Docker only."""

    def __init__(self, backend: ContainerBackend) -> None:
        self.backend = backend
        # Multiple shared members may admit their first turns concurrently.
        # Only one of them may perform the singleton helper's cleanup/start
        # sequence; otherwise two healthy turns can restart each other's
        # observer before either prompt reaches the resident CLI.
        self._guest_observer_lock = threading.RLock()
        # Per-container cursors prevent journal rotation from invalidating a
        # concurrent shared-live turn. Values retain the observer pid too, so
        # a helper restart makes stale cursor bookkeeping self-cleaning.
        self._guest_observer_active: dict[str, dict[int, int]] = {}

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
        network_phase: Literal["setup", "agent"] = "setup",
    ) -> list[str]:
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
        for key, value in sorted(spec.docker_labels().items()):
            argv += ["--label", f"{key}={value}"]
        argv += [
            "--workdir",
            CHAT_WORKDIR,
            "--user",
            f"{self.backend.uid}:{self.backend.gid}",
            # Ordinary new chats begin in setup egress. Reconstructed shared
            # groups may already have completed setup and therefore start on
            # the narrower agent network; widening them again would be a
            # security regression.
            "--network",
            self.backend.network_for_phase(network_phase),
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
        if spec.shared_live:
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
            # Docker builds its default seccomp filter from the container's
            # bounding capabilities.  A later ``docker exec --privileged`` gets
            # every effective capability but does not replace that filter. Keep
            # BPF in the bounding set so the helper's syscall is admitted. The
            # container and every ordinary exec run as non-root with an empty
            # permitted/effective set, and no-new-privileges prevents turning
            # this bounding bit into authority.
            argv += [
                "--cap-add",
                "BPF",
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
        network_phase: Literal["setup", "agent"] = "setup",
    ) -> ChatSandboxInspection:
        """Create one chat container idempotently, never replacing an existing one."""
        existing = self.inspect(spec.container_name)
        if existing is not None:
            self._verify_identity(existing, spec)
            return existing
        command = self.build_create_command(spec, image=image, network_phase=network_phase)
        created = self._run(command, timeout=120)
        if created.returncode != 0:
            # A racing replica may have won after our inspect.  Only accept the
            # resulting container after checking every ownership label.
            raced = self.inspect(spec.container_name)
            if raced is None:
                raise SandboxError(
                    "chat sandbox could not be created: " + created.stderr.strip()[:300]
                )
            self._verify_identity(raced, spec)
            return raced
        inspected = self.inspect(spec.container_name)
        if inspected is None:
            raise SandboxError("Docker created the chat sandbox but it cannot be inspected")
        self._verify_identity(inspected, spec)
        return inspected

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

    def inspect(self, container: str) -> ChatSandboxInspection | None:
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
            networks = ((raw.get("NetworkSettings") or {}).get("Networks") or {}).keys()
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
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxError("Docker returned an invalid chat sandbox inspection") from exc
        self._verify_mounts(inspection)
        return inspection

    def list(self) -> list[ChatSandboxInspection]:
        """List only containers explicitly owned by this lifecycle."""
        result = self._run(
            [
                self.backend.docker_binary,
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                f"label={CHAT_SANDBOX_LABEL}=true",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox listing failed: " + result.stderr.strip()[:300])
        found: list[ChatSandboxInspection] = []
        for container_id in result.stdout.split():
            item = self.inspect(container_id)
            if item is not None:
                found.append(item)
        return found

    def start(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        """Start the exact stopped container and verify it reached RUNNING."""
        current = self._require(spec)
        if current.running:
            return current
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("a paused chat sandbox is not a valid stopped activation")
        if current.status is ChatSandboxStatus.DEAD:
            raise SandboxError("a dead chat sandbox cannot be resumed")
        result = self._run([self.backend.docker_binary, "start", current.container_id], timeout=120)
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be started: " + result.stderr.strip()[:300])
        started = self._require(spec)
        if not started.running or started.status is not ChatSandboxStatus.RUNNING:
            raise SandboxError(
                f"chat sandbox started into unexpected state {started.status.value!r}"
            )
        return started

    def stop(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        """Deactivate with ``docker stop`` and retain the same writable layer."""
        current = self._require(spec)
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("refusing to treat a paused chat sandbox as cleanly stopped")
        if not current.running:
            return current
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
        return stopped

    def require_network_phase(self, spec: ChatSandboxSpec, phase: str) -> None:
        """Prove this container is attached to exactly the requested network."""
        expected = self.backend.network_for_phase(phase)
        current = self._require(spec)
        if set(current.networks) != {expected}:
            raise SandboxError(
                f"retained chat network is {list(current.networks)!r}, not "
                f"the required {phase} network {expected!r}"
            )

    def transition_to_agent_network(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        """Irreversibly replace setup egress with the agent-only network."""
        setup_network = self.backend.network_for_phase("setup")
        agent_network = self.backend.network_for_phase("agent")
        current = self._require(spec)
        if set(current.networks) == {agent_network}:
            return current
        if set(current.networks) != {setup_network}:
            raise SandboxError(
                "retained chat has an unexpected network set before agent transition"
            )
        if setup_network == agent_network:
            return current
        connected = self._run(
            [
                self.backend.docker_binary,
                "network",
                "connect",
                agent_network,
                current.container_id,
            ],
            timeout=30,
        )
        if connected.returncode != 0:
            raise SandboxError(
                "retained chat could not attach agent egress: " + connected.stderr.strip()[:300]
            )
        disconnected = self._run(
            [
                self.backend.docker_binary,
                "network",
                "disconnect",
                setup_network,
                current.container_id,
            ],
            timeout=30,
        )
        if disconnected.returncode != 0:
            # Both networks may now be present, so no caller is allowed to run
            # the Agent CLI: require_network_phase fails closed.
            raise SandboxError(
                "retained chat could not remove setup egress: " + disconnected.stderr.strip()[:300]
            )
        transitioned = self._require(spec)
        if set(transitioned.networks) != {agent_network}:
            raise SandboxError("retained chat agent egress transition was not exact")
        return transitioned

    def build_exec_command(
        self,
        spec: ChatSandboxSpec,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        interactive: bool = False,
        tty: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
    ) -> list[str]:
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
        command += [spec.container_name, *argv]
        return command

    def spawn(
        self,
        spec: ChatSandboxSpec,
        argv: list[str],
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
        if spec.shared_live:
            if member_match is None:
                raise ValueError("process-scoped kill requires a shared member PID file")
            slot_id = int(member_match.group(1))
            if slot_id >= spec.resident_slot_capacity:
                raise ValueError("resident PID file is outside sandbox capacity")
            if user_uid != MEMBER_UID_BASE + slot_id:
                raise ValueError("resident PID file does not match its member uid")
            return slot_id
        legacy_match = re.fullmatch(
            rf"{re.escape(ACTIVATION_TMPFS)}/agent-[0-9a-f]{{32}}\.pid",
            pidfile,
        )
        if legacy_match is None or user_uid != self.backend.uid:
            raise ValueError("process-scoped kill requires a retained agent PID file")
        return -1

    def reap_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Kill unattached live CLI trees in exactly one private member slot."""
        if not spec.shared_live or not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        directory = f"{MEMBER_TMPFS_ROOT}/{slot_id}"
        script = self._member_reaper_script(f"{directory}/resident-*.pid")
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", str(user_uid)],
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
        # A tool can deliberately escape the resident process group with
        # setsid/double-fork, and a broker restart can lose its pidfile. The
        # member UID is the second, independent ownership fence: before opening
        # a replacement transport there must be no process left under it.
        self.reap_member_processes_except(
            spec,
            slot_id=slot_id,
            user_uid=user_uid,
            user_gid=user_gid,
            baseline=(),
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
        script = (
            'set -eu; expected_uid="$1"; for proc in /proc/[0-9]*; do '
            'pid=${proc##*/}; [ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
            'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
            '[ "$uid" = "$expected_uid" ] || continue; '
            'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'started=$(set -- $fields; printf "%s" "${20:-}"); '
            'case "$started" in ""|*[!0-9]*) continue;; esac; '
            'printf "%s %s\n" "$pid" "$started"; done'
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", str(user_uid)],
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
        payload = self._encode_member_processes(baseline)
        receipt = f"{MEMBER_TMPFS_ROOT}/{slot_id}/reap-{secrets.token_hex(16)}.baseline"
        script = (
            'set -eu; expected_uid="$1"; baseline="$2"; umask 077; '
            'cleanup() { rm -f -- "$baseline"; }; trap cleanup EXIT HUP INT TERM; '
            'cat > "$baseline"; '
            'scan() { signal="$1"; extra=0; for proc in /proc/[0-9]*; do '
            'pid=${proc##*/}; [ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
            'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
            '[ "$uid" = "$expected_uid" ] || continue; '
            'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'started=$(set -- $fields; printf "%s" "${20:-}"); '
            'case "$started" in ""|*[!0-9]*) continue;; esac; '
            'grep -Fqx -- "$pid $started" "$baseline" && continue; extra=1; '
            '[ "$signal" = NONE ] || kill -"$signal" "$pid" 2>/dev/null || :; '
            'done; [ "$extra" = 0 ]; }; '
            'round=0; while [ "$round" -lt 20 ]; do '
            "scan TERM && break; round=$((round + 1)); sleep 0.05; done; "
            "round=0; while ! scan KILL; do round=$((round + 1)); "
            '[ "$round" -lt 64 ] || exit 45; sleep 0.05; done; '
            "scan NONE; cleanup; trap - EXIT HUP INT TERM"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", str(user_uid), receipt],
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
        if not spec.shared_live or not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")

    @staticmethod
    def _parse_member_processes(payload: bytes) -> tuple[tuple[int, int], ...]:
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("member process census is not ASCII") from exc
        found: list[tuple[int, int]] = []
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
        if not spec.shared_live:
            raise ValueError("shared workspace preparation requires shared_live")
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
        if not spec.shared_live or not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        # Codex, OpenCode, and Kilo each need a member-private provider home:
        # Codex can make temp files owner-only, while the HTTP runtimes keep
        # SQLite databases and per-project process locks. A sibling UID must
        # never traverse those homes merely to normalize its own writes.
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
            f"-o -path {CHAT_STATE_DIR}/codex-members "
            f"-o -path {CHAT_STATE_DIR}/opencode-members "
            f"-o -path {CHAT_STATE_DIR}/kilo-members \\) -prune -o "
            '-uid "$uid" -type d -exec chmod g+rwx,g+s -- {} +; '
            f"find {CHAT_STATE_DIR} -xdev "
            f"\\( -path {CHAT_STATE_DIR}/.platform "
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
        slot_ids: list[int],
        token: str,
    ) -> str:
        """Stop every existing shared-workspace writer for one short capture.

        Agent processes use per-member UIDs, while terminals/setup commands
        use the sandbox UID.  Freezing the complete existing UID census closes
        the detached-child gap that a process-group-only barrier leaves.  The
        Git capture exec starts after this census and is the sole new writer;
        admissions remain blocked by the supervisor until thaw completes.
        """
        if not spec.shared_live:
            raise ValueError("writer freeze requires a shared-live sandbox")
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
        frozen: list[tuple[int, int, int]] = []
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
        slot_ids: list[int],
        manifest: str,
        require_exact_census: bool = True,
    ) -> None:
        """Resume exactly the frozen PID/start pairs and detect new writers."""
        if not spec.shared_live:
            raise ValueError("writer thaw requires a shared-live sandbox")
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
    ) -> list[tuple[int, int, int]]:
        """Validate the private PID/start-time receipt before any signal."""
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("shared snapshot writer census is not ASCII") from exc
        frozen: list[tuple[int, int, int]] = []
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
    def _encode_frozen_processes(frozen: list[tuple[int, int, int]]) -> bytes:
        """Encode a deterministic, private manifest for the later thaw."""
        return "".join(f"{pid} {uid} {started}\n" for pid, uid, started in sorted(frozen)).encode(
            "ascii"
        )

    def _resume_frozen_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        target_uids: list[int],
        frozen: list[tuple[int, int, int]],
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
        errors: list[str] = []
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
        shared_umask = "umask 0002; " if spec.shared_live else ""
        script = (
            'pidfile="$1"; '
            'printf "%s\\n" "$$" > "$pidfile" || exit 126; '
            'cleanup() { rm -f "$pidfile"; }; '
            "trap 'cleanup' EXIT; "
            f"{shared_umask}"
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
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        timeout_s: float = 60,
        input_bytes: bytes | None = None,
        user_uid: int | None = None,
        user_gid: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one bounded command and capture bytes without opening a host path."""
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        command = self.build_exec_command(
            spec,
            argv,
            env=env,
            workdir=workdir,
            interactive=input_bytes is not None,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        try:
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
        argv: list[str],
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
            spec.container_name,
            *argv,
        ]
        return self._run(command, timeout=timeout_s)

    def _observer_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: list[str],
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
    ):
        """Read an atomic status/size/guest-clock tuple from the root journal."""
        try:
            return self._guest_observer_controller(spec).snapshot(after_poll_seq=after_poll_seq)
        except GuestObserverRuntimeError as exc:
            raise SandboxError(str(exc)) from exc

    def _guest_observer_controller(self, spec: ChatSandboxSpec) -> GuestObserverController:
        """Bind the shared observer contract to this label-verified container."""

        def capture(argv: list[str], timeout_s: float):
            return self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=timeout_s,
                user_uid=0,
                user_gid=0,
            )

        def privileged(argv: list[str], detach: bool, timeout_s: float):
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
    ) -> GuestObserverCursor:
        """Fence the journal immediately before one member prompt is admitted."""
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

    def import_archive(self, spec: ChatSandboxSpec, path: str, archive: bytes) -> None:
        """Write a validated caller-provided tar through Docker's archive boundary."""
        self._require(spec)
        self._validate_container_path(path)
        result = self._run_bytes(
            [
                self.backend.docker_binary,
                "cp",
                "-",
                f"{spec.container_name}:{path}",
            ],
            timeout=120,
            input_bytes=archive,
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

    @staticmethod
    def _verify_identity(inspection: ChatSandboxInspection, spec: ChatSandboxSpec) -> None:
        expected = spec.docker_labels()
        mismatched = {
            key: (inspection.labels.get(key), value)
            for key, value in expected.items()
            if inspection.labels.get(key) != value
        }
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
    def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
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
        argv: list[str], *, timeout: float, input_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv,
                env={**os.environ},
                input=input_bytes,
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
        self._stderr_chunks: list[str] = []
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
        """Kill only one shared member, or use the legacy container backstop."""
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


class RetainedChatBackend(SandboxBackend):
    """Present one retained container through the runner's sandbox contract.

    This adapter is what lets existing runtime parsing, setup, patch building,
    resource sampling and Agent CLI drivers execute with ``docker exec`` while
    their orchestration is migrated incrementally.  It never interprets a
    host ``workdir`` or ``state_dir``: both canonical paths already live in the
    container writable layer.
    """

    name = "retained-container"

    def __init__(self, manager: ChatSandboxManager, spec: ChatSandboxSpec) -> None:
        self.manager = manager
        self.spec = spec
        self.image = spec.sandbox_image_digest or manager.backend.image
        self.runtime = manager.backend.runtime

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

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Execute one Agent CLI or bounded command in the retained container."""
        self.manager.require_network_phase(self.spec, spec.phase)
        env = dict(spec.env)
        if self.manager.backend.egress is not None:
            env.update(self.manager.backend.egress.sandbox_env(spec.phase))
        observed_pidfile = (
            f"{ACTIVATION_TMPFS}/agent-{secrets.token_hex(16)}.pid"
            if spec.observe_processes and self.runtime == KATA_RUNTIME
            else None
        )
        return self.manager.spawn(
            self.spec,
            spec.argv,
            env=env,
            workdir=CHAT_WORKDIR,
            live_input=spec.live_input,
            user_uid=self.manager.backend.uid,
            user_gid=self.manager.backend.gid,
            process_pidfile=observed_pidfile,
        )

    def begin_guest_observation(self) -> GuestObserverCursor | None:
        """Fence this retained turn when it runs inside a Kata guest."""
        if self.runtime != KATA_RUNTIME:
            return None
        return self.manager.begin_guest_observation(
            self.spec,
            user_uid=self.manager.backend.uid,
        )

    def finish_guest_observation(
        self,
        cursor: GuestObserverCursor | None,
    ) -> GuestObservation | None:
        """Close the retained Kata fence; non-Kata backends report no observer."""
        if self.runtime != KATA_RUNTIME:
            return None
        return self.manager.finish_guest_observation(self.spec, cursor)

    def tool_mark_reader(self) -> ToolMarkReader:
        """Read exact Claude hook marks from the container-owned state layer."""
        return self.manager.tool_mark_reader(self.spec)

    def install_tool_hooks(self, update: dict[str, Any]) -> None:
        """Install Claude's passive boundaries in Docker-owned state."""
        self.manager.install_tool_hooks(self.spec, update)

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

    def adopt_workdir(self, path: str) -> None:
        """Docker-layer paths are created for the sandbox uid in the image."""
        if path not in {CHAT_WORKDIR, CHAT_STATE_DIR}:
            raise SandboxError(f"retained backend does not own host path {path!r}")

    def preflight(self, workdir_root: str | None = None) -> None:
        """Use the container backend checks without probing a host bind mount."""
        self.manager.backend.preflight(workdir_root=None)

    def check_gateway_reachable(self, base_url: str) -> None:
        """Delegate the backend's isolated gateway reachability probe."""
        self.manager.backend.check_gateway_reachable(base_url)


__all__ = [
    "ACTIVATION_TMPFS",
    "CHAT_CONTAINER_PREFIX",
    "CHAT_GENERATION_LABEL",
    "CHAT_IMAGE_DIGEST_LABEL",
    "CHAT_RESIDENT_CAPACITY_LABEL",
    "CHAT_RUNTIME_LABEL",
    "CHAT_RUNTIME_VERSION_LABEL",
    "CHAT_SANDBOX_ID_LABEL",
    "CHAT_SANDBOX_LABEL",
    "CHAT_SHARED_LABEL",
    "CHAT_STATE_DIR",
    "CHAT_THREAD_LABEL",
    "CHAT_WORKDIR",
    "MEMBER_TMPFS_ROOT",
    "MEMBER_UID_BASE",
    "MEMBER_UID_LIMIT",
    "ChatSandboxInspection",
    "ChatSandboxManager",
    "ChatSandboxSpec",
    "ChatSandboxStatus",
    "RetainedChatBackend",
    "RetainedChatLost",
]
