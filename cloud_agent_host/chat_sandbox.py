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

from cloud_agent_host.sandbox import (
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxProcess,
    SandboxSpec,
    TerminalProcess,
)

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
CHAT_FORK_OPERATION_LABEL = "org.freeinference.chat-fork-operation"
CHAT_FORK_SOURCE_SANDBOX_LABEL = "org.freeinference.chat-fork-source-sandbox"
CHAT_CONTAINER_PREFIX = "freeinference-chat"
CHAT_WORKDIR = "/workspace"
CHAT_STATE_DIR = "/agent-state"
ACTIVATION_TMPFS = "/run/agent-activation"
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_DOCKER_IMAGE_ID = re.compile(r"sha256:[0-9a-fA-F]{64}")

# PID 1 contains no chat credential and owns no chat state outside the
# container writable layer.  A reconnectable protocol bridge may later replace
# this small loop as a latency optimization; continuity does not depend on it.
# The loop is still preferable to ``tail -f /dev/null`` because it handles
# Docker's SIGTERM deterministically and reaps its one child.
_INIT_COMMAND = (
    "trap 'exit 0' TERM INT; while :; do sleep 3600 & child=$!; wait $child || true; done"
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
            # Ordinary new chats begin in setup egress. A full-sandbox clone
            # has already completed setup and is created directly on the
            # narrower agent network; putting it back on setup egress would be
            # both unnecessary and a security regression.
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
            f"{self.backend.uid}:{self.backend.gid}",
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
    ) -> SandboxProcess:
        """Run an attached command inside the retained container."""
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        command = self.build_exec_command(
            spec,
            argv,
            env=env,
            workdir=workdir,
            interactive=live_input,
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
        return _RetainedExecProcess(process, manager=self, spec=spec)

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

    def commit(self, spec: ChatSandboxSpec, repository: str) -> str:
        """Snapshot a settled container for a platform-owned full sandbox fork."""
        current = self._require(spec)
        if not repository or any(char.isspace() or char == "\x00" for char in repository):
            raise ValueError("snapshot repository must be one Docker image reference")
        result = self._run(
            [
                self.backend.docker_binary,
                "commit",
                "--pause=true",
                current.container_id,
                repository,
            ],
            timeout=300,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox commit failed: " + result.stderr.strip()[:300])
        # Docker Desktop 4.4x writes a ``--pause`` deprecation notice to
        # stdout before the actual receipt. Never feed that untrusted prose
        # back into ``docker create`` as an image reference: select exactly one
        # structurally valid content id instead.
        image_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        image_ids = [line for line in image_ids if _DOCKER_IMAGE_ID.fullmatch(line)]
        if len(image_ids) != 1:
            raise SandboxError("chat sandbox commit returned an invalid image id receipt")
        return image_ids[0]

    def flush_for_snapshot(self, spec: ChatSandboxSpec) -> None:
        """Flush a running, settled container's filesystem before ``commit``.

        Native CLI processes have already exited at the settled boundary. The
        explicit sync closes the remaining kernel writeback gap; Docker's
        pause barrier then freezes the writable layer while it is committed.
        A stopped container is already quiescent and cannot be ``exec``-ed.
        """
        current = self._require(spec)
        if not current.running:
            return
        result = self.exec_capture(spec, ["/bin/sync"], workdir="/", timeout_s=60)
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox snapshot flush failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def remove(
        self,
        spec: ChatSandboxSpec,
        *,
        container_id: str,
    ) -> bool:
        """Remove only an exact, label-verified incomplete clone container.

        This is intentionally not a general chat cleanup API. Callers must
        present the immutable target spec and the exact Docker object id, so a
        stale rollback cannot delete a newer container occupying the name.
        """
        current = self.inspect(spec.container_name)
        if current is None:
            return False
        self._verify_identity(current, spec)
        if current.container_id != container_id:
            raise SandboxError("refusing to remove a different retained chat container")
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("refusing to remove a paused retained chat container")
        if current.running:
            current = self.stop(spec)
        result = self._run(
            [self.backend.docker_binary, "container", "rm", current.container_id],
            timeout=60,
        )
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox rollback removal failed: " + result.stderr.strip()[:300]
            )
        if self.inspect(spec.container_name) is not None:
            raise SandboxError("Docker reported clone removal but the container still exists")
        return True

    @staticmethod
    def snapshot_repository(operation_id: str) -> str:
        """Return a deterministic, Docker-safe temporary tag for one fork saga."""
        if not operation_id or "\x00" in operation_id or len(operation_id) > 256:
            raise ValueError("fork operation id must be a non-empty opaque id")
        digest = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        return f"freeinference-chat-fork:{digest}"

    def remove_image_reference(self, repository: str) -> bool:
        """Untag one platform-owned snapshot without forcing layer deletion."""
        if not repository or any(char.isspace() or char == "\x00" for char in repository):
            raise ValueError("snapshot repository must be one Docker image reference")
        inspected = self._run(
            [self.backend.docker_binary, "image", "inspect", repository],
            timeout=30,
        )
        if inspected.returncode != 0:
            lowered = inspected.stderr.lower()
            if "no such image" in lowered or "no such object" in lowered:
                return False
            raise SandboxError("snapshot image inspect failed: " + inspected.stderr.strip()[:300])
        removed = self._run(
            [self.backend.docker_binary, "image", "rm", repository],
            timeout=60,
        )
        if removed.returncode != 0:
            raise SandboxError("snapshot image untag failed: " + removed.stderr.strip()[:300])
        return True

    def image_id(self, repository: str) -> str | None:
        """Resolve a deterministic snapshot tag for crash-safe fork retries."""
        if not repository or any(char.isspace() or char == "\x00" for char in repository):
            raise ValueError("snapshot repository must be one Docker image reference")
        inspected = self._run(
            [
                self.backend.docker_binary,
                "image",
                "inspect",
                "--format={{.Id}}",
                repository,
            ],
            timeout=30,
        )
        if inspected.returncode != 0:
            lowered = inspected.stderr.lower()
            if "no such image" in lowered or "no such object" in lowered:
                return None
            raise SandboxError("snapshot image inspect failed: " + inspected.stderr.strip()[:300])
        image_id = inspected.stdout.strip()
        if not image_id:
            raise SandboxError("snapshot image inspection returned no image id")
        return image_id

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
    """An attached ``docker exec`` whose kill backstop stops, never removes, chat state."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
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
        """Stop the whole activation so a detached exec cannot mutate state."""
        try:
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
        return self.manager.spawn(
            self.spec,
            spec.argv,
            env=env,
            workdir=CHAT_WORKDIR,
            live_input=spec.live_input,
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
    "CHAT_FORK_OPERATION_LABEL",
    "CHAT_FORK_SOURCE_SANDBOX_LABEL",
    "CHAT_GENERATION_LABEL",
    "CHAT_IMAGE_DIGEST_LABEL",
    "CHAT_RUNTIME_LABEL",
    "CHAT_RUNTIME_VERSION_LABEL",
    "CHAT_SANDBOX_ID_LABEL",
    "CHAT_SANDBOX_LABEL",
    "CHAT_STATE_DIR",
    "CHAT_THREAD_LABEL",
    "CHAT_WORKDIR",
    "ChatSandboxInspection",
    "ChatSandboxManager",
    "ChatSandboxSpec",
    "ChatSandboxStatus",
    "RetainedChatBackend",
    "RetainedChatLost",
]
