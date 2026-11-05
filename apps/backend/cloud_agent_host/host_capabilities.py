"""What this host advertises, and the proof it attaches to every claim.

The shared-live capabilities are proved from this process's own facts -- the
image's labels and pins, the sandbox manager, the slots and the drivers --
never from a sibling replica's heartbeat.
"""

from __future__ import annotations

import os
import pathlib
import secrets
from typing import TYPE_CHECKING, Any

from cloud_agent_host.host_runtime import HOST_CONCURRENCY_CAPABILITY, default_slots
from cloud_agent_host.local_settings import _sandbox_resource_setting
from cloud_agent_host.runtimes import get_runtime, registered_runtimes
from cloud_agent_host.runtimes.live import driver_for
from cloud_agent_host.sandbox import ContainerBackend, SandboxBackend
from cloud_agent_shared import sandbox_archive_settings
from cloud_agent_shared.environment import ENVIRONMENT_CAPABILITY, ENVIRONMENT_INFERENCE_CAPABILITY
from cloud_agent_shared.environment_snapshots import ENVIRONMENT_SNAPSHOT_CAPABILITY
from cloud_agent_shared.model_connections import HOST_MODEL_CONNECTION_CAPABILITIES
from cloud_agent_shared.sandbox_manager_compat import manager_env
from cloud_agent_shared.sandbox_restore import SANDBOX_RESTORE_CAPABILITY
from cloud_agent_shared.shared_protocol import (
    GOAL_REPORT_CAPABILITY,
    HOST_SETTINGS_CAPABILITY,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    PROFILING_CONTROL_CAPABILITY,
    SANDBOX_BROWSER_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    mid_turn_input_capability,
    shared_deployment_capability,
)
from cloud_agent_shared.ssh_access import SSH_ACCESS_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Mapping


def _shared_live_ready_file() -> str:
    """The deployment-readiness marker this runner was started with.

    Written by ``sandbox_host.sh`` — ``pending:<sha>`` while a rollout is still
    checking the host, the bare ``<sha>`` once it has passed — and mounted
    read-only into every replica. Read on every beat for the exact-SHA
    capability, and watched between beats so the control plane's rollout fence
    hears about that SHA as soon as it is published rather than at the next
    scheduled heartbeat.
    """
    return (os.environ.get("AGENT_SHARED_LIVE_READY_FILE") or "").strip()


def _steerable_runtime_capabilities() -> list[str] | None:
    """Mid-turn input capabilities of the registered runtimes.

    None when any registered runtime cannot run as a shared-live resident.
    """
    steerable: list[str] = []
    for runtime_id in registered_runtimes():
        try:
            runtime_capabilities = driver_for(get_runtime(runtime_id)).capabilities()
        except KeyError:
            return None
        if (
            runtime_capabilities.resident_transport != "supervisor_live"
            or runtime_capabilities.fork != "at_anchor"
            or not runtime_capabilities.concurrent_shared_workspace
        ):
            return None
        if runtime_capabilities.steering == "mid_turn":
            steerable.append(mid_turn_input_capability(runtime_id))
    return steerable


def _shared_live_host_capabilities(
    backend: SandboxBackend,
    stamp: dict[str, Any],
) -> list[str]:
    """The capabilities this replica may advertise, given what it proves.

    Empty means "not a shared-live host at all". Register, beat and claim all
    read this one list, so a replica that cannot prove the sandbox contract
    cannot advertise anything that depends on it either.
    """
    if not isinstance(backend, ContainerBackend):
        return []
    # One child process runs one claimed turn, and the host claims only while
    # it is under its limit, so slots can never outnumber the processes that
    # honour them. What can still be wrong is the floor: shared native forks
    # need a sibling running beside its parent, and a limit of one would
    # serialize them. The default is validated here because the deployment
    # check attests through this function before any turn has been claimed.
    try:
        default_slots()
    except ValueError:
        return []
    # Sessions per sandbox is no longer checked here: the local value is only
    # a bootstrap, an unusable one falls back to the default
    # (local_settings.local_shared_resident_capacity), and an admin's value is validated
    # before it is saved and again when it arrives.
    if not (manager_env("AGENT_SANDBOX_MANAGER_URL") or "").strip():
        return []
    if not (manager_env("AGENT_SANDBOX_MANAGER_TOKEN") or "").strip():
        return []
    digest = stamp.get("sandbox_image_digest")
    if not isinstance(digest, str) or not digest.strip():
        return []
    versions = stamp.get("runtime_versions")
    if not isinstance(versions, dict) or any(
        versions.get(runtime_id) != version
        for runtime_id, version in SHARED_SANDBOX_RUNTIME_PINS.items()
    ):
        return []
    helpers = stamp.get("helper_versions")
    if not isinstance(helpers, dict) or any(
        helpers.get(helper_id) != version
        for helper_id, version in SHARED_SANDBOX_HELPER_PINS.items()
    ):
        return []
    steerable = _steerable_runtime_capabilities()
    if steerable is None:
        return []
    capabilities = [
        SHARED_SANDBOX_CAPABILITY,
        NATIVE_FORK_AT_ANCHOR_CAPABILITY,
        # A live resident session is also what makes a turn's final message
        # readable: the settle path that ends the turn is the one that reports
        # it whole, before anything bounds it. This host therefore never
        # advertises the goal report without the contract it rides on.
        GOAL_REPORT_CAPABILITY,
        # Named per runtime: the sandbox manager of this same deployment takes a
        # message into a running turn only through a driver that wires it.
        *steerable,
        # This exact replica can accept the deployment-wide DB archive policy
        # over its authenticated sandbox manager channel. A claim also attests the
        # exact revision that sandbox manager confirmed.
        sandbox_archive_settings.SANDBOX_ARCHIVE_CAPABILITY,
        # The same-deployment sandbox manager understands the policy's
        # checkpoint switch and reports checkpoint receipts.
        sandbox_archive_settings.SANDBOX_CHECKPOINT_CAPABILITY,
        # ... and archives to whichever bucket the policy names.
        sandbox_archive_settings.SANDBOX_ARCHIVE_DESTINATION_CAPABILITY,
        # ... and recreates a group another host left behind from its
        # checkpoint, so a claim may hand this replica a cold group.
        SANDBOX_RESTORE_CAPABILITY,
        *([SANDBOX_BROWSER_CAPABILITY] if helpers.get("cloud-agent-browser") else []),
    ]
    deployment_sha = (os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip()
    ready_file = _shared_live_ready_file()
    if deployment_sha and ready_file:
        try:
            ready_sha = pathlib.Path(ready_file).read_text().strip()
        except OSError:
            ready_sha = ""
        if secrets.compare_digest(ready_sha, deployment_sha):
            capabilities.append(shared_deployment_capability(deployment_sha))
    return capabilities


def _environment_host_capabilities(backend: SandboxBackend, stamp: dict[str, Any]) -> list[str]:
    """Advertise environment execution and SSH access only beside proven shared-live support."""
    capabilities = _shared_live_host_capabilities(backend, stamp)
    if not capabilities:
        return []
    return [
        *capabilities,
        ENVIRONMENT_CAPABILITY,
        ENVIRONMENT_INFERENCE_CAPABILITY,
        ENVIRONMENT_SNAPSHOT_CAPABILITY,
        SSH_ACCESS_CAPABILITY,
        *HOST_MODEL_CONNECTION_CAPABILITIES,
    ]


def _advertised_host_capabilities(backend: SandboxBackend, stamp: dict[str, Any]) -> list[str]:
    """What this host advertises, at enrollment and on every heartbeat alike.

    The concurrency, profiling-control and host-settings capabilities say this
    runner applies ``slots_desired``, a runtime profiling configuration and the
    admin's host settings. They let the admin page tell a host that will pick
    up a new value from one that needs a redeploy. They are advertised beside
    the shared-live capabilities, never instead of them.
    """
    return [
        *_environment_host_capabilities(backend, stamp),
        HOST_CONCURRENCY_CAPABILITY,
        PROFILING_CONTROL_CAPABILITY,
        HOST_SETTINGS_CAPABILITY,
    ]


def _shared_live_deployment_ready(
    backend: SandboxBackend,
    stamp: dict[str, Any],
) -> bool:
    """Whether this replica may poll, including against the previous API.

    The target API enforces the exact-SHA capability at claim time, but the
    previous API in a parallel rollout cannot know that new field yet. A
    pending runner must therefore refrain from polling locally until the host
    checks publish its exact marker. It still enrolls and heartbeats with the
    base protocol capabilities so those checks can inspect the replacement.
    """
    deployment_sha = (os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip()
    if not deployment_sha:
        return False
    return shared_deployment_capability(deployment_sha) in _shared_live_host_capabilities(
        backend, stamp
    )


def claim_attestation(
    backend: SandboxBackend,
    *,
    sandbox_archive_settings_applied_revision: int | None = None,
    host_settings: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Build the exact-replica proof required for every claim.

    ``host_settings`` are the settings a turn claimed now would be handed;
    absent ones are read from this process's environment, as the turn does.
    """
    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    # ``version_stamp`` proves the immutable image. This separately proves the
    # polling process has the sandbox manager, slots and driver surfaces
    # needed to use it; a sibling replica's host heartbeat cannot stand in for
    # those process-local facts during a rolling deployment.
    stamp["runner_capabilities"] = _environment_host_capabilities(backend, stamp)
    # The disk quota a group created by this claim would get, so the control
    # plane can warn about a new untrusted group with none. Zero, or a value
    # the turn would refuse, is no quota.
    try:
        stamp["sandbox_disk_limit_mib"] = _sandbox_resource_setting(
            "sandbox_disk_mib", host_settings or {}
        )
    except ValueError:
        stamp["sandbox_disk_limit_mib"] = 0
    if sandbox_archive_settings_applied_revision is not None:
        stamp["sandbox_archive_settings_applied_revision"] = (
            sandbox_archive_settings_applied_revision
        )
    return stamp
