"""Host-wide discovery of containers that depend on the packet boundary."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cloud_agent_host.chat_sandbox_models import (
    CHAT_CPU_LIMIT_LABEL,
    CHAT_DISK_LIMIT_LABEL,
    CHAT_GENERATION_LABEL,
    CHAT_IMAGE_DIGEST_LABEL,
    CHAT_MEMORY_LIMIT_LABEL,
    CHAT_PROBE_LABEL,
    CHAT_RESIDENT_CAPACITY_LABEL,
    CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SHARED_LABEL,
    CHAT_STOP_TIMEOUT_LABEL,
    CHAT_THREAD_LABEL,
    ENVIRONMENT_TEST_LABEL,
    ChatSandboxInspection,
    ChatSandboxSpec,
)
from cloud_agent_host.idle_host_rules import running_deployment_container_ids
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cloud_agent_host.chat_sandbox import ChatSandboxManager

logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")

_MALFORMED_STOP_TIMEOUT_S = 10


def exact_stop_unidentified(manager: ChatSandboxManager, container_id: str) -> bool:
    """Stop an owned running ID whose full inspection cannot be trusted."""
    try:
        result = manager._run(
            [
                manager.backend.docker_binary,
                "stop",
                "--time",
                str(_MALFORMED_STOP_TIMEOUT_S),
                container_id,
            ],
            timeout=_MALFORMED_STOP_TIMEOUT_S + 30,
        )
        if result.returncode != 0:
            raise SandboxError(
                "owned container could not be fail-stopped: " + result.stderr.strip()[:300]
            )
    except Exception:
        logger.exception(
            "unidentified unguarded sandbox could not be exact-stopped",
            extra={
                "event": "sandbox_network_unidentified_stop_failed",
                "container_id": container_id[:12],
            },
        )
        return False
    return True


@dataclass(frozen=True)
class GuardedWorkload:
    """One exact running container whose bridge relies on the host rules."""

    spec: ChatSandboxSpec
    inspection: ChatSandboxInspection
    environment_run: bool


@dataclass(frozen=True)
class GuardedInventory:
    """One host-wide discovery result, retaining uncertainty and failed stops."""

    workloads: tuple[GuardedWorkload, ...] = ()
    unresolved_container_ids: tuple[str, ...] = ()
    complete: bool = True


class KnownGuardedGroup(Protocol):
    """Supervisor state needed to conservatively retain an exact identity."""

    spec: ChatSandboxSpec
    inspection: ChatSandboxInspection
    starting: bool
    stopping: bool


def sandbox_spec_from_inspection(inspection: ChatSandboxInspection) -> ChatSandboxSpec:
    """Rebuild immutable sandbox identity from Docker-owned labels."""
    labels = inspection.labels
    try:
        if labels.get(CHAT_SHARED_LABEL) != "true":
            raise ValueError("not a shared-live sandbox")
        environment_run = labels.get(ENVIRONMENT_TEST_LABEL) or None
        return ChatSandboxSpec(
            thread_id=labels[CHAT_THREAD_LABEL],
            sandbox_id=labels[CHAT_SANDBOX_ID_LABEL],
            generation=int(labels[CHAT_GENERATION_LABEL]),
            runtime_id=labels[CHAT_RUNTIME_LABEL],
            runtime_version=labels.get(CHAT_RUNTIME_VERSION_LABEL) or None,
            sandbox_image_digest=labels.get(CHAT_IMAGE_DIGEST_LABEL) or None,
            memory_limit=labels[CHAT_MEMORY_LIMIT_LABEL],
            cpu_limit=labels[CHAT_CPU_LIMIT_LABEL],
            disk_limit_mib=int(labels.get(CHAT_DISK_LIMIT_LABEL, "0")),
            stop_timeout_s=int(labels[CHAT_STOP_TIMEOUT_LABEL]),
            shared_live=True,
            resident_slot_capacity=int(labels[CHAT_RESIDENT_CAPACITY_LABEL]),
            labels=(
                {ENVIRONMENT_TEST_LABEL: environment_run} if environment_run is not None else {}
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SandboxError(
            f"shared container {inspection.name!r} has incomplete identity labels"
        ) from exc


def running_guarded_workloads(
    manager: ChatSandboxManager,
) -> GuardedInventory:
    """Discover workloads without erasing unavailable or incomplete outcomes."""
    container_ids = running_deployment_container_ids(manager)
    if container_ids is None:
        return GuardedInventory(complete=False)
    workloads: list[GuardedWorkload] = []
    unresolved: list[str] = []
    for container_id in container_ids:
        try:
            inspection = manager.inspect(container_id)
            if (
                inspection is None
                or not inspection.running
                or inspection.labels.get(CHAT_PROBE_LABEL)
            ):
                continue
            spec = sandbox_spec_from_inspection(inspection)
        except Exception:
            if not exact_stop_unidentified(manager, container_id):
                unresolved.append(container_id)
            continue
        workloads.append(
            GuardedWorkload(
                spec=spec,
                inspection=inspection,
                environment_run=bool(inspection.labels.get(ENVIRONMENT_TEST_LABEL)),
            )
        )
    return GuardedInventory(
        workloads=tuple(workloads),
        unresolved_container_ids=tuple(unresolved),
    )


def workloads_with_known(
    inventory: GuardedInventory,
    groups: Iterable[KnownGuardedGroup],
) -> list[GuardedWorkload]:
    """Merge discovery with exact supervisor identities during uncertain lifecycle states."""
    workloads = list(inventory.workloads)
    seen = {workload.inspection.container_id for workload in workloads}
    for group in groups:
        inspection = group.inspection
        if not (inspection.running or group.starting or group.stopping):
            continue
        if inspection.container_id in seen:
            continue
        workloads.append(GuardedWorkload(group.spec, inspection, False))
        seen.add(inspection.container_id)
    return workloads


def merge_guarded_inventories(
    previous: GuardedInventory,
    latest: GuardedInventory,
) -> GuardedInventory:
    """Retain exact earlier identities while adding a post-failure snapshot."""
    workloads = list(previous.workloads)
    seen = {workload.inspection.container_id for workload in workloads}
    for workload in latest.workloads:
        if workload.inspection.container_id not in seen:
            workloads.append(workload)
            seen.add(workload.inspection.container_id)
    unresolved = latest.unresolved_container_ids
    if not latest.complete:
        unresolved = tuple(
            dict.fromkeys((*previous.unresolved_container_ids, *latest.unresolved_container_ids))
        )
    return GuardedInventory(tuple(workloads), unresolved, latest.complete)


def host_rule_failure(
    manager: ChatSandboxManager,
    workloads: Iterable[GuardedWorkload],
) -> str | None:
    """Freshly read back the host boundary twice and return its final failure."""
    specs = [workload.spec for workload in workloads]
    detail = ""
    for _attempt in range(2):
        try:
            manager.network.verify_host_rules(specs)
        except Exception as exc:
            detail = str(exc)[:300]
        else:
            return None
    return detail


def retry_unresolved_stops(manager: ChatSandboxManager, inventory: GuardedInventory) -> None:
    """Retry every unidentified ID after the host boundary cannot be proven."""
    for container_id in inventory.unresolved_container_ids:
        exact_stop_unidentified(manager, container_id)


def exact_stop_unguarded(
    manager: ChatSandboxManager,
    workload: GuardedWorkload,
    *,
    release_networks: bool,
) -> bool:
    """Stop one discovered exact id, falling back when structured inspection fails."""
    try:
        manager.stop(
            workload.spec,
            expected_container_id=workload.inspection.container_id,
            release_networks=release_networks,
        )
    except Exception:
        logger.exception(
            "unguarded sandbox could not be exact-stopped",
            extra={
                "event": "sandbox_network_exact_stop_failed",
                "sandbox_id": workload.spec.sandbox_id,
                "generation": workload.spec.generation,
                "container_id": workload.inspection.container_id[:12],
                "environment_run": workload.environment_run,
            },
        )
        return exact_stop_unidentified(manager, workload.inspection.container_id)
    return True


__all__ = [
    "GuardedInventory",
    "GuardedWorkload",
    "exact_stop_unguarded",
    "exact_stop_unidentified",
    "host_rule_failure",
    "merge_guarded_inventories",
    "retry_unresolved_stops",
    "running_guarded_workloads",
    "sandbox_spec_from_inspection",
    "workloads_with_known",
]
