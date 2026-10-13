"""Retained container identities, ownership labels, inspected state, and lifecycle errors.

The value objects here contain no Docker commands or process state. Execution
and network reconciliation remain in :mod:`cloud_agent_host.chat_sandbox`,
which reexports these names for existing callers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from cloud_agent_shared.errors import SandboxError

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
CHAT_DISK_LIMIT_LABEL = "org.freeinference.chat-disk-limit-mib"
CHAT_STOP_TIMEOUT_LABEL = "org.freeinference.chat-stop-timeout-seconds"
CHAT_SHARED_LABEL = "org.freeinference.chat-shared-live"
CHAT_RESIDENT_CAPACITY_LABEL = "org.freeinference.chat-resident-slot-capacity"
#: Marks a disposable preflight probe with its creation time (Unix seconds).
#: Retained-container discovery skips it: the sandbox manager adopted probes as orphan
#: chats and stopped them mid-probe, failing the host's runtime qualification.
CHAT_PROBE_LABEL = "org.freeinference.chat-probe-created"
CHAT_NETWORK_LABEL = "org.freeinference.chat-sandbox-network"
CHAT_NETWORK_SANDBOX_ID_LABEL = "org.freeinference.chat-sandbox-id"
CHAT_NETWORK_GENERATION_LABEL = "org.freeinference.chat-generation"
CHAT_NETWORK_SERVICES_LABEL = "org.freeinference.chat-service-aliases"
CHAT_NETWORK_POOL_LABEL = "org.freeinference.chat-network-pool"
CHAT_CONTAINER_PREFIX = "freeinference-chat"

MEMBER_UID_BASE = 11001
MEMBER_UID_LIMIT = 128


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


class RetainedChatFailStopped(SandboxError):
    """Activation failed after the exact retained container was cleanly stopped."""

    def __init__(self, detail: str, inspection: ChatSandboxInspection) -> None:
        super().__init__(detail)
        self.inspection = inspection


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
    disk_limit_mib: int = 0
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
        if (
            not isinstance(self.disk_limit_mib, int)
            or isinstance(self.disk_limit_mib, bool)
            or not 0 <= self.disk_limit_mib <= 16777216
        ):
            raise ValueError("disk_limit_mib must be a whole number between 0 and 16777216")
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
            CHAT_DISK_LIMIT_LABEL,
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
        return self.container_name_for(self.thread_id, self.sandbox_id)

    @staticmethod
    def container_name_for(thread_id: str, sandbox_id: str) -> str:
        """Derive the stable container name from its two durable identities."""
        digest = hashlib.sha256(f"{thread_id}\0{sandbox_id}".encode()).hexdigest()[:32]
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
        if self.disk_limit_mib:
            labels[CHAT_DISK_LIMIT_LABEL] = str(self.disk_limit_mib)
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
    started_at: str = ""


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


__all__ = [
    "CHAT_CONTAINER_PREFIX",
    "CHAT_CPU_LIMIT_LABEL",
    "CHAT_DISK_LIMIT_LABEL",
    "CHAT_GENERATION_LABEL",
    "CHAT_IMAGE_DIGEST_LABEL",
    "CHAT_MEMORY_LIMIT_LABEL",
    "CHAT_NAMESPACE_LABEL",
    "CHAT_NETWORK_GENERATION_LABEL",
    "CHAT_NETWORK_LABEL",
    "CHAT_NETWORK_POOL_LABEL",
    "CHAT_NETWORK_SANDBOX_ID_LABEL",
    "CHAT_NETWORK_SERVICES_LABEL",
    "CHAT_PROBE_LABEL",
    "CHAT_RESIDENT_CAPACITY_LABEL",
    "CHAT_RUNTIME_LABEL",
    "CHAT_RUNTIME_VERSION_LABEL",
    "CHAT_SANDBOX_ID_LABEL",
    "CHAT_SANDBOX_LABEL",
    "CHAT_SHARED_LABEL",
    "CHAT_STOP_TIMEOUT_LABEL",
    "CHAT_THREAD_LABEL",
    "MEMBER_UID_BASE",
    "MEMBER_UID_LIMIT",
    "ChatSandboxInspection",
    "ChatSandboxSpec",
    "ChatSandboxStatus",
    "RetainedChatFailStopped",
    "RetainedChatLost",
    "SandboxActionFacts",
]
