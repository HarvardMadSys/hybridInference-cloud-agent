"""What a turn asks of its sandbox group, checked and reserved before the attempt starts.

Nothing here touches a container: a turn this host cannot run is refused
before any work, and the control plane reserves the group's identity and
limits under the attempt's fence.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.claimed_job import ClaimedJob, LeaseLost
from cloud_agent_host.local_settings import (
    _sandbox_resource_setting,
    local_shared_resident_capacity,
)
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.shared_protocol import FORK_HELPER, PLATFORM_MCP_SERVER_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.control_plane_client import ControlPlane
    from cloud_agent_host.sandbox import SandboxBackend

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


def owner_visible_error(exc: Exception) -> str:
    """Describe a failure for the chat's owner without the control plane's address.

    The owner reads a failed turn's detail, and httpx names the URL it called
    in every status error, which put this deployment's internal control-plane
    address in front of them. A status error says what the control plane
    answered instead; the log line written beside the detail keeps its type
    and status for an operator. Other failures read as they always have.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"the control plane answered HTTP {exc.response.status_code}"
    return str(exc)


#: What the owner reads when their chat uses connected tools and the host that
#: ran it has no MCP relay. The fault is the deployment's, and only an admin can
#: fix it, so this says that instead of naming a variable the owner cannot set.
SITE_MISCONFIGURED_MCP = (
    "Site misconfigured: this chat uses connected tools, but the server that ran it "
    "has no MCP relay. Ask an admin to finish setting up the site."
)


#: The execution profile for public, untrusted work. Spelled out here because
#: the host cannot import the control plane's ``ExecutionProfile``.
UNTRUSTED_EXECUTION_PROFILE = "untrusted-kata-v1"


#: What the runner logs when it starts a new untrusted group without a disk
#: quota. Recommended, not required: the group's writable layer holds its
#: workspace and every installed file and survives ``docker stop``, so without
#: one a single tenant can fill the disk every other group on this host writes
#: to. The fix is the deployment's, so this names the setting.
UNTRUSTED_DISK_QUOTA_RECOMMENDED = (
    "this untrusted sandbox has no container disk quota: set Container disk "
    "(sandbox_disk_mib) above 0 for this server, on Docker overlay2 backed by XFS "
    "with project quotas"
)


class GroupRefused(Exception):
    """The turn cannot run here; nothing has started, and the attempt is finished failed."""


@dataclass(frozen=True)
class GroupPlan:
    """What a turn asks of its sandbox group before the control plane reserves it."""

    job: ClaimedJob
    mcp_url: str
    manager: ChatSandboxManager
    stamp: dict[str, Any]
    existing: dict[str, Any]
    capacity: int
    origin_thread_id: str
    seed_spec: ChatSandboxSpec


def plan_group(
    job: ClaimedJob,
    *,
    backend: SandboxBackend,
    host: str | None,
    settings: Mapping[str, float],
) -> GroupPlan:
    """Check the turn can run here and describe the group it will reserve."""
    if not isinstance(backend, ContainerBackend):
        raise GroupRefused("shared live chats require the container or kata backend")
    if not host or not job.thread_id:
        raise GroupRefused("shared live chats require an enrolled host and thread id")
    mcp_url = _sandbox_mcp_url()
    if job.mcp_bindings and not mcp_url:
        if any(not alias.startswith(PLATFORM_MCP_SERVER_PREFIX) for alias in job.mcp_bindings):
            # Before any container work: the runtime would refuse these tools only
            # at the resident launch, after a checkout the turn could never use.
            raise GroupRefused(SITE_MISCONFIGURED_MCP)
        # Only the platform's own tools, which every turn carries: without a
        # relay the turn runs without them rather than every chat failing.
        job = replace(job, mcp_bindings=[])

    manager = ChatSandboxManager(backend)
    # Only the image's facts are read here. The downloaded versions take a
    # Docker listing of seconds, which the version check below makes only
    # when it must.
    stamp = backend.version_stamp(downloads="skip")
    existing = job.thread_sandbox or {}
    _refuse_a_changed_group(job, existing, stamp)

    # An existing group keeps the capacity it was created with: its member
    # slots are built into the container. Only a new group takes the setting.
    capacity = int(
        existing.get("resident_slot_capacity")
        or settings.get("shared_resident_capacity")
        or local_shared_resident_capacity()
    )
    if not 2 <= capacity <= 128:
        raise GroupRefused("shared resident capacity must be between 2 and 128")
    memory_limit, cpu_limit, disk_limit_mib = _group_limits(job, existing, settings, host=host)
    sandbox_id = str(existing.get("sandbox_id") or f"asbx_{secrets.token_hex(16)}")
    origin_thread_id = str(existing.get("origin_thread_id") or job.thread_id)
    seed_spec = ChatSandboxSpec(
        thread_id=origin_thread_id,
        sandbox_id=sandbox_id,
        generation=int(existing.get("generation") or 1),
        runtime_id=str(existing.get("runtime_id") or job.runtime),
        runtime_version=(existing.get("runtime_version") if existing else job.runtime_version),
        sandbox_image_digest=(
            existing.get("sandbox_image_digest")
            if existing
            else (stamp.get("sandbox_image_digest") or None)
        ),
        shared_live=True,
        resident_slot_capacity=capacity,
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        disk_limit_mib=disk_limit_mib,
        stop_timeout_s=int(existing.get("stop_timeout_seconds") or 10),
    )
    if existing:
        if existing.get("host") != host:
            raise GroupRefused("shared chat was claimed on the wrong host")
        if existing.get("container_name") != seed_spec.container_name:
            raise GroupRefused("shared chat container identity does not match its labels")
    return GroupPlan(
        job=job,
        mcp_url=mcp_url,
        manager=manager,
        stamp=stamp,
        existing=existing,
        capacity=capacity,
        origin_thread_id=origin_thread_id,
        seed_spec=seed_spec,
    )


def _refuse_a_changed_group(
    job: ClaimedJob, existing: dict[str, Any], stamp: dict[str, Any]
) -> None:
    """Refuse a group the control plane describes as something this turn cannot reuse."""
    if existing and existing.get("shared_live") is not True:
        raise GroupRefused("the control plane returned a non-shared sandbox mapping")
    if not existing and not str(stamp.get("sandbox_image_digest") or "").strip():
        raise GroupRefused("shared live chat creation requires an inspected image digest")
    if existing and existing.get("runtime_id") != job.runtime:
        raise GroupRefused(f"this shared chat is pinned to runtime {existing.get('runtime_id')!r}")
    if existing and existing.get("execution_profile") != job.execution_profile:
        raise GroupRefused("shared chat execution profile cannot change in place")


def _group_limits(
    job: ClaimedJob,
    existing: dict[str, Any],
    settings: Mapping[str, float],
    *,
    host: str,
) -> tuple[str, str, int]:
    """The group's memory, CPU and disk limits: its own, or this turn's settings for a new one."""
    if existing:
        # Resource limits belong to the retained group. A settings edit must
        # not change its reservation or a fork's shared container. Legacy
        # groups have no disk quota, including when a new default is set.
        return (
            str(existing.get("memory_limit") or "4g"),
            str(existing.get("cpu_limit") or "2"),
            int(existing.get("disk_limit_mib") or 0),
        )
    try:
        memory_limit = f"{_sandbox_resource_setting('sandbox_memory_mib', settings)}m"
        cpu_limit = f"{_sandbox_resource_setting('sandbox_cpu_millicores', settings) / 1000:g}"
        disk_limit_mib = _sandbox_resource_setting("sandbox_disk_mib", settings)
    except ValueError as exc:
        raise GroupRefused(str(exc)) from exc
    if job.execution_profile == UNTRUSTED_EXECUTION_PROFILE and not disk_limit_mib:
        # A warning, not a refusal: the group starts as configured.
        logger.warning(
            UNTRUSTED_DISK_QUOTA_RECOMMENDED,
            extra={
                "event": "agent_untrusted_sandbox_without_disk_quota",
                "job_id": job.job_id,
                "host": host,
            },
        )
    return memory_limit, cpu_limit, disk_limit_mib


def reserve_group(
    plan: GroupPlan,
    *,
    control: ControlPlane,
    host: str | None,
    measurement: operation_profiling.Measurement | None,
) -> dict[str, Any]:
    """Reserve the group under this attempt's fence and check the limits it confirms."""
    seed_spec = plan.seed_spec
    operation_profiling.mark(measurement, "reserve")
    try:
        reservation = control.reserve_chat_sandbox(
            {
                "sandbox_id": seed_spec.sandbox_id,
                "container_name": seed_spec.container_name,
                "runtime_id": seed_spec.runtime_id,
                "runtime_version": seed_spec.runtime_version,
                "sandbox_image_digest": seed_spec.sandbox_image_digest,
                "execution_profile": plan.job.execution_profile,
                "host": host,
                "shared_live": True,
                "resident_slot_capacity": plan.capacity,
                "memory_limit": seed_spec.memory_limit,
                "cpu_limit": seed_spec.cpu_limit,
                "disk_limit_mib": seed_spec.disk_limit_mib,
                "stop_timeout_seconds": seed_spec.stop_timeout_s,
                # What the image a new container is created from declares, so
                # Fork can ask it of this group after the host moves on to a
                # newer image. An existing group recorded its own already.
                "fork_helper_version": (
                    None
                    if plan.existing
                    else (plan.stamp.get("helper_versions") or {}).get(FORK_HELPER)
                ),
            }
        )
    except LeaseLost:
        raise
    except Exception as exc:
        raise GroupRefused(
            f"could not reserve shared chat sandbox: {owner_visible_error(exc)}"
        ) from exc

    if (
        reservation.get("memory_limit") != seed_spec.memory_limit
        or reservation.get("cpu_limit") != seed_spec.cpu_limit
        or reservation.get("disk_limit_mib", 0) != seed_spec.disk_limit_mib
    ):
        raise GroupRefused(
            "Site misconfigured: the control plane did not confirm the reserved "
            "container CPU, memory and disk limits. Update the control plane and host together."
        )
    return reservation


def _sandbox_mcp_url() -> str:
    """This host's MCP relay, as its sandboxes reach it; empty when it has none.

    The host compose derives it from the host's own MCP tunnel, as it does the
    gateway's address, and it is the relay the host rules admit for every
    group (``egress.network_from_env``). The control plane used to hand one
    over at claim time from a setting of its own. Nothing kept that copy in
    step with the host, and left unset it failed every turn whose owner had
    connected a tool.
    """
    return (os.environ.get("AGENT_SANDBOX_MCP_URL") or "").strip()
