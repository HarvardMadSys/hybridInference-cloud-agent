"""One attempt of a turn in its shared-live sandbox group.

The attempt reserves the group with the control plane, acquires it from the
sandbox manager, prepares the workspace, starts or reuses the member's
resident Agent CLI, admits and streams the prompt, and settles both sides.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.attempt_lease import (
    Heartbeater,
    _retry_control_receipt,
    _retry_lifecycle_receipt,
)
from cloud_agent_host.chat_sandbox import (
    CHAT_UPLOADS_DIR,
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatBackend,
)
from cloud_agent_host.claimed_job import (
    ClaimedJob,
    LeaseLost,
    model_connection_launch,
    require_residency_connection,
)
from cloud_agent_host.control_plane_client import ControlPlane, _log_control_failure
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.github_relay_env import residency_github_launch
from cloud_agent_host.local_settings import (
    _chat_idle_timeout_s,
    _sandbox_resource_setting,
    local_shared_resident_capacity,
)
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import DELIVERY_HANDOFF_DRAIN_S
from cloud_agent_host.measurement_sender import _enqueue_delivery
from cloud_agent_host.resident_probe import probe_live_resident
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.resources import shared_member_resource_rollup
from cloud_agent_host.runtimes import AgentRuntime, NormalizedEvent
from cloud_agent_host.sandbox import ContainerBackend, SandboxBackend, SandboxError
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
    session_supervisor_from_env,
)
from cloud_agent_host.setup import run_setup
from cloud_agent_host.source_preparation import (
    GitSourcePrefetch,
    WorktreeError,
    _choose_environment_snapshot,
    _import_retained_base,
    _prefetch_retained_source,
    _resident_base_ready,
    _retained_head,
)
from cloud_agent_host.turn_stream import _reportable_turn_resources, _stream_shared_agent_turn
from cloud_agent_host.upload_import import import_uploads as _import_uploads
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.sandbox_manager_compat import SANDBOX_MANAGER_PRODUCER
from cloud_agent_shared.sandbox_manager_wire import member_manifest
from cloud_agent_shared.sandbox_restore import RESTORE_FAILED_RELEASE_REASON
from cloud_agent_shared.shared_protocol import FORK_HELPER, PLATFORM_MCP_SERVER_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


def _owner_visible_error(exc: Exception) -> str:
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


def runtime_version_unavailable(job: ClaimedJob, backend: SandboxBackend) -> str | None:
    """Why this host cannot run the version the attempt is stamped with, if so.

    Normally unreachable: the control plane refuses a selection no host offers,
    and the claim falls back when the *claiming* host does not list it. What is
    left is drift — a host rebuilt with a shorter menu, claiming in the window
    before its heartbeat says so.

    Checked against the version stamp rather than by probing inside the
    container: the image's labels, which ``install-runtimes.sh`` writes from
    the same argument it installs, plus the versions this host downloaded since
    an admin updated the agent, each kept only after its check passed.

    The image's own menu, and a retained chat's pinned version, answer without
    Docker. Only a version found in neither lists the downloaded ones, which
    takes seconds on a host holding many images, and it lists them anew: a menu
    read earlier could predate the download, and must not fail the turn.
    """
    version = job.runtime_version
    if not version:
        # No selection reached this attempt — an older control plane, or a host
        # that never reported a version. The image default is exactly right.
        return None
    stamper = getattr(backend, "version_stamp", None)
    if stamper is None:
        return None
    available = (stamper(downloads="skip").get("runtime_versions_available") or {}).get(job.runtime)
    if available is not None and version in available:
        return None
    available = (stamper(downloads="fresh").get("runtime_versions_available") or {}).get(
        job.runtime
    )
    if available is None or version in available:
        return None
    return (
        f"this attempt is stamped {job.runtime} {version}, which this host does not "
        f"hold (it offers: {', '.join(available) or 'nothing'}). Update the agent to "
        "that version again from the admin Settings page so this host downloads it, "
        "or choose another version there."
    )


def _verified_native_session_id(
    *,
    previous_session_id: str | None,
    observed_session_id: str | None,
    has_context: bool,
) -> str:
    """Return the exact CLI locator only when native continuity was proved.

    A successful exit is not enough for a retained follow-up: some CLIs can
    recover from an invalid ``resume`` argument by creating a fresh session.
    Accepting that process would silently turn RESTORED/LIVE into transcript-
    free context loss. Every supported adapter reports its native locator, so
    resumed turns must report the same one they were asked to continue.
    """
    observed = str(observed_session_id or "").strip()
    if not observed:
        raise SandboxError("Agent CLI completed without a native session locator")
    previous = str(previous_session_id or "").strip()
    if has_context and observed != previous:
        raise SandboxError(
            "Agent CLI resumed a different native session "
            f"(expected {previous!r}, observed {observed!r})"
        )
    return observed


def _shared_group_manifest(reservation: dict[str, Any], *, thread_id: str) -> list[dict[str, Any]]:
    """Project the durable member manifest onto the sandbox manager's strict wire shape."""
    raw = reservation.get("group_members")
    members = [dict(item) for item in raw or [] if isinstance(item, dict)]
    if not members:
        members = [dict(reservation)]
    projected = member_manifest(members)
    if not any(member.get("thread_id") == thread_id for member in projected):
        raise SandboxError("shared sandbox manifest omitted the claimed chat")
    for member in projected:
        if member.get("resident_slot_id") is None:
            raise SandboxError("shared sandbox manifest contains an unassigned resident slot")
    return projected


def _classify_startup_path(container_action: str, resident_action: str) -> str:
    """Name the startup path from the actions the supervisor actually took.

    The four paths differ by what was done, not by how long it took, and the
    container's state afterwards cannot tell them apart: a stop followed by a
    start and a container that never stopped both end up running. So this reads
    the supervisor's report rather than inferring from the timeline, and says
    ``unknown`` when the actions do not identify a path instead of guessing.
    """
    if container_action == "create":
        return "new_group"
    if container_action == "start":
        return "resume_stopped"
    # Anything else -- including a resident action the sandbox manager did not report --
    # does not identify a path, and guessing one would poison the comparison
    # the four paths exist for.
    if container_action == "reuse":
        if resident_action == "reuse":
            return "reuse_live"
        if resident_action in {"new", "resume"}:
            return "resume_running"
    return "unknown"


def _stage(measurement: operation_profiling.Measurement | None, name: str) -> str:
    """Record a stage boundary and return the name.

    The runner already tracks its phase in ``stage`` for failure reporting, so
    the timeline costs one call per transition and needs no restructuring.
    Consecutive marks become the record's per-stage durations.
    """
    operation_profiling.mark(measurement, name)
    return name


class _HandBack(Exception):
    """The sandbox manager refused the turn before anything ran; the turn goes back.

    It could not recreate a moved group, or the group stayed busy past its
    wait. ``reason`` is the release reason the control plane records.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _run_shared_live_attempt(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
    *,
    runtime: AgentRuntime,
    job: ClaimedJob,
    control: ControlPlane,
    heart: Heartbeater,
    backend: SandboxBackend,
    host: str | None,
    agent_timeout_s: float,
    finish_attempt: Any,
    measurement: operation_profiling.Measurement | None = None,
    host_settings: Mapping[str, float] | None = None,
) -> int:
    """Deliver one chat turn to its supervisor-owned resident CLI member.

    ``measurement`` is the opt-in startup timing for this attempt. It is
    ``None`` unless an experiment selected it, and every call on it is a no-op
    in that case, so the delivery path is unchanged when profiling is off.

    ``host_settings`` are the runtime settings the runner resolved for this
    turn — an admin's values over the host's own. A setting absent from it is
    read from this process's environment, as a turn started by hand does.
    """
    settings = host_settings or {}
    operation_profiling.mark(measurement, "validate")
    if not isinstance(backend, ContainerBackend):
        finish_attempt("failed", "shared live chats require the container or kata backend")
        return 2
    if not host or not job.thread_id:
        finish_attempt("failed", "shared live chats require an enrolled host and thread id")
        return 2
    mcp_url = _sandbox_mcp_url()
    if job.mcp_bindings and not mcp_url:
        if any(not alias.startswith(PLATFORM_MCP_SERVER_PREFIX) for alias in job.mcp_bindings):
            # Before any container work: the runtime would refuse these tools only
            # at the resident launch, after a checkout the turn could never use.
            finish_attempt("failed", SITE_MISCONFIGURED_MCP)
            return 2
        # Only the platform's own tools, which every turn carries: without a
        # relay the turn runs without them rather than every chat failing.
        job = replace(job, mcp_bindings=[])

    manager = ChatSandboxManager(backend)
    # Only the image's facts are read here. The downloaded versions take a
    # Docker listing of seconds, which the version check below makes only
    # when it must.
    stamp = backend.version_stamp(downloads="skip")
    existing = job.thread_sandbox or {}
    if existing and existing.get("shared_live") is not True:
        finish_attempt("failed", "the control plane returned a non-shared sandbox mapping")
        return 2
    if not existing and not str(stamp.get("sandbox_image_digest") or "").strip():
        finish_attempt("failed", "shared live chat creation requires an inspected image digest")
        return 2
    if existing and existing.get("runtime_id") != job.runtime:
        finish_attempt(
            "failed",
            f"this shared chat is pinned to runtime {existing.get('runtime_id')!r}",
        )
        return 2
    if existing and existing.get("execution_profile") != job.execution_profile:
        finish_attempt("failed", "shared chat execution profile cannot change in place")
        return 2

    # An existing group keeps the capacity it was created with: its member
    # slots are built into the container. Only a new group takes the setting.
    capacity = int(
        existing.get("resident_slot_capacity")
        or settings.get("shared_resident_capacity")
        or local_shared_resident_capacity()
    )
    if not 2 <= capacity <= 128:
        finish_attempt("failed", "shared resident capacity must be between 2 and 128")
        return 2
    if existing:
        # Resource limits belong to the retained group. A settings edit must
        # not change its reservation or a fork's shared container. Legacy
        # groups have no disk quota, including when a new default is set.
        memory_limit = str(existing.get("memory_limit") or "4g")
        cpu_limit = str(existing.get("cpu_limit") or "2")
        disk_limit_mib = int(existing.get("disk_limit_mib") or 0)
    else:
        try:
            memory_limit = f"{_sandbox_resource_setting('sandbox_memory_mib', settings)}m"
            cpu_limit = f"{_sandbox_resource_setting('sandbox_cpu_millicores', settings) / 1000:g}"
            disk_limit_mib = _sandbox_resource_setting("sandbox_disk_mib", settings)
        except ValueError as exc:
            finish_attempt("failed", str(exc))
            return 2
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
            finish_attempt("failed", "shared chat was claimed on the wrong host")
            return 2
        if existing.get("container_name") != seed_spec.container_name:
            finish_attempt("failed", "shared chat container identity does not match its labels")
            return 2

    operation_profiling.mark(measurement, "reserve")
    try:
        reservation = control.reserve_chat_sandbox(
            {
                "sandbox_id": seed_spec.sandbox_id,
                "container_name": seed_spec.container_name,
                "runtime_id": seed_spec.runtime_id,
                "runtime_version": seed_spec.runtime_version,
                "sandbox_image_digest": seed_spec.sandbox_image_digest,
                "execution_profile": job.execution_profile,
                "host": host,
                "shared_live": True,
                "resident_slot_capacity": capacity,
                "memory_limit": seed_spec.memory_limit,
                "cpu_limit": seed_spec.cpu_limit,
                "disk_limit_mib": seed_spec.disk_limit_mib,
                "stop_timeout_seconds": seed_spec.stop_timeout_s,
                # What the image a new container is created from declares, so
                # Fork can ask it of this group after the host moves on to a
                # newer image. An existing group recorded its own already.
                "fork_helper_version": (
                    None if existing else (stamp.get("helper_versions") or {}).get(FORK_HELPER)
                ),
            }
        )
    except LeaseLost:
        return 3
    except Exception as exc:
        finish_attempt(
            "failed", f"could not reserve shared chat sandbox: {_owner_visible_error(exc)}"
        )
        return 2

    if (
        reservation.get("memory_limit") != seed_spec.memory_limit
        or reservation.get("cpu_limit") != seed_spec.cpu_limit
        or reservation.get("disk_limit_mib", 0) != seed_spec.disk_limit_mib
    ):
        finish_attempt(
            "failed",
            "Site misconfigured: the control plane did not confirm the reserved "
            "container CPU, memory and disk limits. Update the control plane and host together.",
        )
        return 2

    spec = ChatSandboxSpec(
        thread_id=str(reservation.get("origin_thread_id") or origin_thread_id),
        sandbox_id=str(reservation["sandbox_id"]),
        generation=int(reservation["generation"]),
        runtime_id=str(reservation["runtime_id"]),
        runtime_version=reservation.get("runtime_version"),
        sandbox_image_digest=reservation.get("sandbox_image_digest"),
        shared_live=True,
        resident_slot_capacity=int(reservation.get("resident_slot_capacity") or capacity),
        memory_limit=str(reservation.get("memory_limit") or seed_spec.memory_limit),
        cpu_limit=str(reservation.get("cpu_limit") or seed_spec.cpu_limit),
        disk_limit_mib=int(reservation.get("disk_limit_mib", seed_spec.disk_limit_mib)),
        stop_timeout_s=int(reservation.get("stop_timeout_seconds") or seed_spec.stop_timeout_s),
    )
    activation_generation = int(reservation["activation_generation"])
    # A group the control plane moved here is recreated from its checkpoint,
    # never created empty: the sandbox manager refuses a moved group without it.
    restore = reservation.get("restore") if reservation.get("state") == "restoring" else None
    if reservation.get("state") == "restoring" and not isinstance(restore, dict):
        finish_attempt("failed", "the control plane moved this chat without its checkpoint")
        return 2
    has_context = bool(reservation.get("last_settled_job_id"))
    native_session_id = str(reservation.get("native_session_id") or "").strip() or None
    manifest = _shared_group_manifest(reservation, thread_id=job.thread_id)
    supervisor: SessionSupervisorClient | None = None
    source: GitSourcePrefetch | None = None
    lease_token: str | None = None
    settled = False
    # The sandbox manager has settled the turn: its session is good whatever
    # the control plane's receipt says next, and no longer leased to this attempt.
    locally_settled = False
    abandoned = False
    turn_admitted = False
    terminals_suspended = False
    context_invalid = False
    failure_detail: str | None = None
    stage = _stage(measurement, "supervisor_acquire")
    context_status = "restored" if has_context else "new"
    idle_timeout_s = float(settings.get("chat_idle_timeout_s") or _chat_idle_timeout_s())

    def abandon_before_finish(*, lost: bool, detail: str | None) -> None:
        nonlocal abandoned
        if abandoned or settled:
            return
        if supervisor is not None and lease_token is not None and not locally_settled:
            try:
                supervisor.abandon_shared(job.thread_id, token=lease_token, lost=lost)
            except Exception as exc:
                _log_control_failure("supervisor_abandon", job.job_id, exc)
        try:
            control.abandon_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                lost=lost,
                detail=detail,
                idle_timeout_seconds=idle_timeout_s,
            )
        except Exception as exc:
            _log_control_failure("sandbox_abandon", job.job_id, exc)
        abandoned = True

    def report_failure() -> None:
        # Telemetry availability must never gate the authoritative transition.
        # Keep trying finish even when the error-event endpoint is unavailable.
        detail = (failure_detail or "shared live execution failed")[:1000]
        try:
            control.append_event(NormalizedEvent("error", {"text": detail}))
        except Exception as exc:
            _log_control_failure("error_event", job.job_id, exc)
        try:
            finish_attempt("failed", detail)
        except Exception as exc:
            _log_control_failure("finish", job.job_id, exc)

    def finish_decided(state: str, detail: str, base_sha: str | None) -> None:
        # The outcome is already decided: both settlements are durable, or the
        # owner's Stop has released or interrupted the turn. The receipt is
        # retried through a brief control-plane error and never downgraded to
        # a failure of its own: an exhausted retry is logged and left to the
        # lease reaper, which ends the job from that durable state (a settled
        # context is kept, a requested cancel stays a cancel) and never
        # replays the prompt. A fence refusing a retry (LeaseLost) is usually
        # the earlier try having landed, and there is nothing left to report
        # either way.
        try:
            _retry_lifecycle_receipt(
                "finish_retry",
                job.job_id,
                lambda: finish_attempt(state, detail, base_sha=base_sha),
            )
        except LeaseLost:
            raise
        except Exception as exc:
            _log_control_failure("finish", job.job_id, exc)

    def stopped_before_admission(base_sha: str | None) -> bool:
        # Nothing has reached the CLI yet, so a Stop costs nothing here: the
        # activation is released with the conversation intact and the job is
        # finished cancelled. Admitting anyway would spend the prompt only to
        # interrupt it, and an interrupted turn leaves its native context
        # lost. The heartbeat may carry the Stop, or a lost lease, at any time
        # while the turn starts, so this is asked after the container starts,
        # around the setup script, and once more right before admission.
        nonlocal failure_detail
        if heart.lease_lost:
            raise LeaseLost("lease lost before prompt admission")
        if not heart.cancel_requested:
            return False
        failure_detail = "cancelled by owner before prompt admission"
        operation_profiling.finish(measurement, outcome="cancelled", failure_stage=stage)
        abandon_before_finish(lost=False, detail=failure_detail)
        finish_decided("cancelled", "cancelled by owner", base_sha)
        return True

    try:
        if not has_context:
            # Clone into a host-local temporary archive while the activation
            # below creates and boots the guest. This cannot mutate the
            # sandbox whose durable initial marker still has to be probed. If
            # that probe recovers a prepared baseline, discard this speculative
            # source instead of importing it.
            import_mib = settings.get("source_import_max_mib")
            source = _prefetch_retained_source(
                job=job,
                uid=manager.backend.uid,
                gid=manager.backend.gid,
                max_bytes=None if import_mib is None else int(import_mib) * 1024 * 1024,
            )
        snapshot, snapshot_outcome = _choose_environment_snapshot(
            job,
            spec,
            source,
            new_group=not has_context and restore is None and not reservation.get("container_id"),
        )
        supervisor = session_supervisor_from_env()
        try:
            acquire_call = operation_profiling.expect_fragment(
                measurement, SANDBOX_MANAGER_PRODUCER, "supervisor_acquire"
            )
            # Before the call, so a proof the sandbox manager makes during it is never
            # taken for younger than it is.
            acquire_started = time.monotonic()
            lease = supervisor.acquire_shared(
                spec,
                thread_id=job.thread_id,
                activation_generation=activation_generation,
                owner=f"attempt:{job.attempt_id}",
                has_settled_context=has_context,
                members=manifest,
                expected_container_id=reservation.get("container_id"),
                profiling=operation_profiling.measurement_context(measurement, acquire_call),
                start_seq=reservation.get("start_seq"),
                restore=restore,
                environment_snapshot=(
                    snapshot.model_dump(mode="json") if snapshot is not None else None
                ),
            )
        except SessionSupervisorError as exc:
            if restore is not None and exc.status_code == 424:
                raise _HandBack(
                    RESTORE_FAILED_RELEASE_REASON, f"sandbox restore failed: {exc}"
                ) from exc
            if exc.status_code == 409:
                # The group stayed busy past the sandbox manager's wait: a
                # snapshot capture can take longer, and the control plane
                # claims a follow-up whatever the group is doing.
                raise _HandBack("sandbox_busy", f"sandbox busy: {exc}") from exc
            context_invalid = has_context and exc.status_code == 410
            raise
        lease_token = str(lease["token"])
        # The container holds a snapshot's installation, whether this attempt
        # or an earlier one created it, and its workspace must be kept.
        from_snapshot = bool(lease.get("environment_snapshot_id"))
        if snapshot is not None:
            snapshot_outcome = (
                "used"
                if lease.get("environment_snapshot_id") == snapshot.snapshot_id
                else str(lease.get("environment_snapshot_failure") or "unavailable")
            )
        container_action = str(lease.get("container_action") or "unknown")
        # One answer for one call. Sub-stages the sandbox manager measured are durations,
        # not offsets, so they hang off the stage they happened in rather than
        # being placed on this process's timeline.
        operation_profiling.record_fragment(
            measurement,
            producer=SANDBOX_MANAGER_PRODUCER,
            call_id=acquire_call,
            stage="supervisor_acquire",
            response=lease,
        )
        if int(lease["activation_generation"]) != activation_generation:
            raise SandboxError("shared supervisor returned the wrong activation generation")
        if reservation.get("container_id") not in {None, lease.get("container_id")}:
            raise SandboxError("shared supervisor returned a replacement chat container")
        context_status = str(lease.get("context_status") or context_status)
        if context_status not in {"new", "live", "restored"}:
            raise SandboxError(f"shared supervisor returned invalid context {context_status!r}")
        stage = _stage(measurement, "sandbox_running")
        running = control.mark_chat_sandbox_running(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            container_id=str(lease["container_id"]),
            context_status=context_status,
        )
        # The sandbox manager proved the group bridge inside that call, the same check
        # the turn's commands would repeat. A sandbox manager that does not say so leaves
        # every command to check for itself, as before.
        retained = RetainedChatBackend(
            manager,
            spec,
            network_proved_at=acquire_started if lease.get("network_verified") is True else None,
        )
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "started",
                    "runtime": job.runtime,
                    "attempt_no": job.attempt_no,
                    "context_status": context_status,
                    "resident_state": lease.get("resident_state"),
                    "workspace_shared": True,
                    "container_id": lease["container_id"],
                    "activation_generation": activation_generation,
                    **{**retained.sandbox_metadata(), **job.started_context_fields()},
                },
            )
        )
        if stopped_before_admission(None):
            return 0
        stage = _stage(measurement, "terminal_suspend")
        control.suspend_terminals("workspace_preparing")
        terminals_suspended = True

        imported_base: str | None = None
        recovered_initial = False
        if not has_context and container_action != "create":
            # A previous activation may have committed the immutable S3
            # baseline and then died before returning its receipt. Resolve
            # that durable marker before reset, setup, permissions, or uploads
            # can alter the exact prepared bytes it names. A container this
            # acquire created holds no marker it wrote, so none to resolve: the
            # capture clears one a checkpoint carried and refuses any other.
            stage = _stage(measurement, "initial_archive_status")
            initial_status = supervisor.initial_archive_status(
                spec,
                thread_id=job.thread_id,
                token=lease_token,
                members=manifest,
                expected_container_id=str(lease["container_id"]),
                preparation_id=job.job_id,
            )
            status_enabled = initial_status.get("enabled")
            status_prepared = initial_status.get("prepared")
            status_stored = initial_status.get("stored")
            valid_status = (
                status_enabled is False and status_prepared is False and status_stored is False
            ) or (status_enabled is True and status_prepared is False and status_stored is False)
            if status_enabled is True and status_prepared is True and status_stored is True:
                recovered_base = initial_status.get("base_sha")
                if isinstance(recovered_base, str) and re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})", recovered_base
                ):
                    base_sha = recovered_base
                    recovered_initial = True
                    reused_workspace = True
                    valid_status = True
            if not valid_status:
                raise SandboxError("sandbox manager returned an invalid initial archive status")
            if recovered_initial and source is not None:
                source.discard()
                source = None

        if not has_context:
            if not recovered_initial:
                # The marker probe above proved there is no prepared baseline.
                # Only now may the prefetched source mutate the sandbox.
                stage = _stage(measurement, "workspace_reset")
                manager.reset_unsettled_state(spec, keep_workspace=from_snapshot)
                stage = _stage(measurement, "checkout")
                if source is None:
                    raise SandboxError("a new chat's source was never prepared")
                base_sha = source.import_into(
                    manager, spec, observer=operation_profiling.stage_observer(measurement)
                )
                if snapshot_outcome is not None:
                    control.report_environment_snapshot(
                        snapshot_id=(
                            None
                            if snapshot_outcome == "absent" or job.environment_snapshot is None
                            else job.environment_snapshot.snapshot_id
                        ),
                        outcome=snapshot_outcome,
                        base_sha=base_sha,
                    )
                reused_workspace = False
                if job.setup_script and job.environment is None:
                    stage = _stage(measurement, "setup")
                    if stopped_before_admission(base_sha):
                        return 0
                    setup = run_setup(
                        script=job.setup_script,
                        workdir=CHAT_WORKDIR,
                        backend=retained,
                        stop=lambda: heart.cancel_requested or heart.lease_lost,
                    )
                    control.append_event(
                        NormalizedEvent(
                            "lifecycle",
                            {"phase": "setup", "ran": setup.ran},
                        )
                    )
                    if stopped_before_admission(base_sha):
                        return 0
                    if setup.exit_code != 0:
                        failure_detail = f"setup failed ({setup.exit_code}): {setup.detail}"
                        abandon_before_finish(lost=False, detail=failure_detail)
                        finish_attempt("failed", failure_detail, base_sha=base_sha)
                        return 1
                stage = _stage(measurement, "workspace_permissions")
                manager.prepare_shared_workspace(spec)
        else:
            stage = _stage(measurement, "native_workspace_resume")
            if not native_session_id:
                context_invalid = True
                raise SandboxError("shared chat has no native Agent CLI session locator")
            if not _resident_base_ready(CHAT_WORKDIR, job, retained):
                imported_base = _import_retained_base(
                    job=job, manager=manager, spec=spec, backend=retained
                )
                if not _resident_base_ready(CHAT_WORKDIR, job, retained):
                    context_invalid = True
                    raise SandboxError("shared chat cannot resolve its pinned base commit")
                control.append_event(
                    NormalizedEvent(
                        "lifecycle",
                        {
                            "phase": "base_imported",
                            "base_sha": imported_base,
                            "workspace_head_preserved": True,
                        },
                    )
                )
            base_sha = job.base_sha or _retained_head(retained)
            reused_workspace = True
        # Every turn, after both branches: a first turn populates a new
        # container and a follow-up re-populates one that may have been rebuilt.
        # Before the turn is admitted, so the agent never observes a half-written
        # directory.
        stage = "uploads"
        imported_uploads = _import_uploads(
            job=job,
            control=control,
            manager=manager,
            spec=spec,
            # A recovered baseline belongs to the same retained container, so
            # its earlier thread uploads are already present. A replacement
            # first-turn job can still add files of its own and must import the
            # thread's current attachment set before prompt admission.
            fresh_container=not has_context and not recovered_initial,
            container_id=str(lease["container_id"]),
        )
        if imported_uploads:
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "uploads_imported",
                        "files": imported_uploads,
                        "path": CHAT_UPLOADS_DIR,
                    },
                )
            )
        if not has_context and not recovered_initial:
            # Source, setup, permissions, and uploaded files are complete, and
            # no resident CLI or prompt exists yet. Capture this point-in-time
            # boundary before admitting either. The sandbox manager owns S3 access; the
            # runner never receives cloud credentials.
            stage = _stage(measurement, "initial_archive")
            initial_archive = supervisor.capture_initial_archive(
                spec,
                thread_id=job.thread_id,
                token=lease_token,
                members=manifest,
                expected_container_id=str(lease["container_id"]),
                preparation_id=job.job_id,
                base_sha=base_sha,
                # The bundle durable on the host fixes the bytes; S3 can follow.
                defer_upload=True,
            )
            archive_enabled = initial_archive.get("enabled")
            archive_stored = initial_archive.get("stored")
            pending = archive_stored is False and initial_archive.get("upload") == "pending"
            if not (
                (archive_enabled is False and archive_stored is False and not pending)
                or (archive_enabled is True and (archive_stored is True or pending))
            ):
                raise SandboxError("sandbox manager returned an invalid initial archive receipt")
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "initial_archive",
                        "enabled": archive_enabled,
                        "stored": archive_stored,
                        **({"upload": "pending"} if pending else {}),
                    },
                )
            )
        elif recovered_initial:
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "initial_archive",
                        "enabled": True,
                        "stored": True,
                        "recovered": True,
                    },
                )
            )
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "checked_out",
                    "base_sha": base_sha,
                    "reused": reused_workspace,
                    "storage": "docker_writable_layer",
                    "workspace_shared": True,
                    # Nothing was cloned: the tree is the one empty commit, and
                    # the owner is told so instead of "repository ready".
                    **({"temporary": True} if job.repo is None else {}),
                },
            )
        )

        stage = _stage(measurement, "runtime_preflight")
        version_problem = runtime_version_unavailable(job, retained)
        if version_problem is not None:
            failure_detail = version_problem
            abandon_before_finish(lost=False, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if lease.get("resident_state") != "live" and not retained.has_binary(runtime.binary):
            failure_detail = f"runtime binary {runtime.binary!r} is not installed"
            abandon_before_finish(lost=False, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 2

        stage = _stage(measurement, "agent_network")
        # This manager runs in the runner's own process, so its sub-stages are
        # timed directly onto the measurement. Setup ran on this same group
        # bridge; nothing is switched here, only proved still in place before
        # the CLI starts, which the activation's proof already does unless
        # setup ran past a minute.
        network_observer = operation_profiling.stage_observer(measurement)
        retained.require_network(observer=network_observer)
        if job.ssh_access is not None:
            # Before setup and the agent: this member holds the keys of its
            # selected servers, which _claim_secret_values already masks, and
            # the bridge reaches them. A newer state the control plane already
            # pushed wins: what it closed stays closed.
            ssh_slot = int(
                running.get("resident_slot_id") or reservation.get("resident_slot_id") or 0
            )
            supervisor.sync_ssh_access(
                job.thread_id, job.ssh_access.with_slot(job.thread_id, ssh_slot), starting=True
            )
        if job.environment is not None:
            try:
                prepared = supervisor.prepare_environment(
                    job.thread_id,
                    job.environment,
                    activation_owner=lease_token,
                    cancelled=lambda: heart.cancel_requested or heart.lease_lost,
                )
            except SessionSupervisorError:
                # A Stop kills the running script, and the preparation then
                # fails. Nothing has reached the CLI yet, so it is no failure.
                if stopped_before_admission(base_sha):
                    return 0
                raise
            control.append_event(
                NormalizedEvent("lifecycle", {"phase": "environment_prepared", **prepared})
            )

        stage = _stage(measurement, "terminal_resume")
        control.resume_terminals()
        terminals_suspended = False

        slot_id = int(running.get("resident_slot_id") or reservation.get("resident_slot_id") or 0)
        launch = ResidentLaunch(
            thread_id=job.thread_id,
            runtime_id=job.runtime,
            runtime_version=job.runtime_version,
            native_session_id=native_session_id,
            model=job.model,
            reasoning_effort=job.reasoning_effort,
            credential=None,
            environment=job.environment,
            github_repo=job.repo or "",
            plugins=tuple(job.plugins),
            mcp_server_ids=tuple(job.mcp_bindings),
            # The MCP credential is process-fixed, and this turn's own token
            # dies with this attempt, so the probe carries none: a live process
            # holds its residency's credential, which serves this turn too
            # (``compatible_with``). Only a process opened below receives one.
            mcp_base_url=mcp_url if job.mcp_bindings else "",
            mcp_credential="",
            sandbox_mcp_servers=job.sandbox_mcp_servers if job.mcp_bindings else (),
            slot_id=slot_id,
            residency_generation=int(running.get("residency_generation") or 0),
            **model_connection_launch(job),
        )
        stage = _stage(measurement, "resident_start")
        resident_descriptor, resident_call = probe_live_resident(
            measurement,
            supervisor,
            lease=lease,
            thread_id=job.thread_id,
            token=lease_token,
            launch=launch,
        )
        if resident_descriptor is None:
            residency = control.establish_shared_residency(
                sandbox_id=spec.sandbox_id,
                runtime_id=job.runtime,
                runtime_version=job.runtime_version,
            )
            credential = str(residency.get("credential") or "")
            if not credential:
                raise SandboxError("control plane returned no shared residency credential")
            # The residency's: this process serves later turns' tools without a restart.
            mcp_credential = str(residency.get("mcp_credential") or "") if job.mcp_bindings else ""
            if job.mcp_bindings and not mcp_credential:
                raise SandboxError("control plane returned no shared residency MCP credential")
            grant_model = residency.get("model")
            if grant_model not in {None, job.model}:
                raise SandboxError("shared residency grant model does not match this turn")
            require_residency_connection(job, residency)
            launch = replace(
                launch,
                credential=credential,
                mcp_credential=mcp_credential,
                residency_generation=int(residency["generation"]),
                **residency_github_launch(job.repo, residency),
            )
            resident_call = operation_profiling.expect_fragment(
                measurement, SANDBOX_MANAGER_PRODUCER, "resident_start"
            )
            resident_descriptor = supervisor.ensure_shared_resident(
                job.thread_id,
                token=lease_token,
                launch=launch,
                profiling=operation_profiling.measurement_context(measurement, resident_call),
            )
        context_status = str(resident_descriptor.get("context_status") or context_status)
        if context_status not in {"new", "live", "restored"}:
            raise SandboxError(
                f"shared supervisor returned invalid resident context {context_status!r}"
            )
        process_generation = int(resident_descriptor["process_generation"])
        resident_action = str(resident_descriptor.get("resident_action") or "unknown")
        operation_profiling.record_fragment(
            measurement,
            producer=SANDBOX_MANAGER_PRODUCER,
            call_id=resident_call,
            stage="resident_start",
            response=resident_descriptor,
        )
        if measurement is not None:
            operation_profiling.set_attributes(
                measurement,
                context_status=context_status,
                container_action=container_action,
                resident_action=resident_action,
                startup_path=_classify_startup_path(container_action, resident_action),
                process_generation=process_generation,
                container_id=str(lease["container_id"]),
                workspace_action=(
                    "prepare" if not has_context else ("base_import" if imported_base else "reuse")
                ),
            )
        continuity: tuple[str | None, str] = (None, "new")
        if context_status == "live":
            continuity = ("hot", "live")
        elif has_context:
            continuity = ("warm_native", "restored")
        control.record_chat_process(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            residency_generation=launch.residency_generation,
            process_generation=process_generation,
            continuity=continuity,
        )

        stage = _stage(measurement, "prompt_admission")
        # The last point at which a Stop costs nothing.
        if stopped_before_admission(base_sha):
            return 0
        _retry_control_receipt(
            "prompt_admission_retry",
            job.job_id,
            lambda: control.admit_chat_turn(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                residency_generation=launch.residency_generation,
            ),
            heart=heart,
        )
        turn_admitted = True
        result_frame: dict[str, Any] | None = None
        try:
            stage = "turn_stream"
            # The prompt handoff is the last startup stage. `stage` stays a
            # plain string because a failure inside the turn is an execution
            # failure, not a startup stage.
            operation_profiling.mark(measurement, "prompt_dispatch")

            def _frozen_measurement(frozen: operation_profiling.Measurement) -> None:
                """Send a startup sample the moment it is complete.

                Its own callback rather than a call here, because the freeze
                happens inside the streaming helper: the startup ends when the
                prompt is handed to the supervisor, not when the model is done.
                """
                _enqueue_delivery(
                    frozen,
                    job=job,
                    # The client already holds the address it was built with,
                    # so this path needs no extra plumbing to reach it.
                    base_url=control._base,
                    drain_s=DELIVERY_HANDOFF_DRAIN_S,
                )

            exit_code, tail, tool_errors, result_frame, reported_failure = (
                _stream_shared_agent_turn(
                    supervisor=supervisor,
                    job=job,
                    control=control,
                    heart=heart,
                    lease_token=lease_token,
                    timeout_s=agent_timeout_s,
                    measurement=measurement,
                    on_frozen=_frozen_measurement,
                )
            )
        finally:
            # Container totals include concurrent siblings and cannot be
            # attributed to this member. The sandbox manager instead returns UID-scoped
            # guest process telemetry and per-turn tool windows; if the stream
            # died before a terminal frame, retain an explicit zero-coverage
            # row rather than inventing a share of the container total.
            with contextlib.suppress(Exception):
                fallback = shared_member_resource_rollup()
                observed = result_frame.get("resources") if isinstance(result_frame, dict) else None
                control.report_resources(observed if isinstance(observed, dict) else fallback)
            # The whole-turn member window travels with the same terminal
            # frame. It is posted here, before the terminal transition below,
            # because the report is fenced on this attempt's live lease -- the
            # same reason the legacy rollup above is posted here. A frame
            # without the key is a turn nobody measured, and is left absent
            # rather than filled with zeros.
            with contextlib.suppress(Exception):
                # The sandbox manager ships the legacy rollup and the turn summary
                # inside one ``resources`` object; reading the frame's top
                # level found nothing and silently reported no turn at all.
                turn_resources = (
                    (result_frame.get("resources") or {}).get("turn_resources")
                    if isinstance(result_frame, dict)
                    and isinstance(result_frame.get("resources"), dict)
                    else None
                )
                reportable = _reportable_turn_resources(turn_resources)
                if reportable is not None:
                    control.report_turn_resources(reportable)
        if exit_code == 130:
            failure_detail = "cancelled by owner after shared-session admission"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_decided("cancelled", "cancelled by owner", base_sha)
            return 0
        if exit_code == 124:
            failure_detail = "shared Agent CLI turn exceeded its execution timeout"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        # A terminal native API error can leave its history and process
        # intact. Only a sandbox manager result emitted after activation cleanup proves
        # this boundary; process death, timeout and lost streams remain LOST.
        settled_failure = (
            exit_code != 0
            and isinstance(result_frame, dict)
            and result_frame.get("kind") == "result"
            and result_frame.get("status") == "failed"
            and result_frame.get("native_turn_settled") is True
        )
        if exit_code != 0 and not settled_failure:
            failure_detail = reported_failure or f"agent exited {exit_code}: {tail[-500:]}"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if tool_errors:
            # Reported, not failed: a successful shared turn may race a
            # sibling, so tool errors alone do not prove this member produced
            # no useful state.
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {"phase": "shared_tool_errors_observed", "count": len(tool_errors)},
                )
            )

        stage = _stage(measurement, "settlement")
        local_settle = _retry_lifecycle_receipt(
            "supervisor_settlement_retry",
            job.job_id,
            lambda: supervisor.settle_shared(
                job.thread_id,
                token=lease_token,
                job_id=job.job_id,
                context_status=context_status,
            ),
        )
        settled_session_id = _verified_native_session_id(
            previous_session_id=native_session_id,
            observed_session_id=local_settle.get("native_session_id"),
            has_context=has_context,
        )
        locally_settled = True
        _retry_control_receipt(
            "control_settlement_retry",
            job.job_id,
            lambda: control.settle_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                residency_generation=launch.residency_generation,
                context_status=context_status,
                native_session_id=settled_session_id,
                base_sha=base_sha,
                idle_timeout_seconds=idle_timeout_s,
            ),
            heart=heart,
        )
        settled = True
        if settled_failure:
            # Record the native turn ordinal before failing the job, so later
            # retries and historical forks keep matching the CLI transcript.
            finish_decided(
                "failed",
                reported_failure or f"agent failed at native turn boundary: {tail[-500:]}",
                base_sha,
            )
            return 1
        try:
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "shared_workspace_settled",
                        "workspace_shared": True,
                    },
                )
            )
        except LeaseLost:
            raise
        except Exception as exc:
            # Both authoritative settlements succeeded. A missing UI notice
            # must not turn that success into a failed terminal transition.
            _log_control_failure("completion_event", job.job_id, exc)
        finish_decided(
            "succeeded",
            "agent completed (shared workspace)",
            base_sha,
        )
        return 0
    except LeaseLost:
        failure_detail = "attempt lease lost during shared live execution"
        operation_profiling.finish(measurement, outcome="lease_lost", failure_stage=stage)
        return 3
    except _HandBack as exc:
        # Nothing ran and the chat is unchanged: release the activation, which
        # gives a group being restored back to its checkpoint, and the turn
        # back to the queue, for this host or another to try again once the
        # control plane's retry delay has passed. Once the control plane has
        # run out of restore attempts it marks the group lost instead, and this
        # turn fails with it.
        failure_detail = str(exc)
        _log_control_failure(stage, job.job_id, exc)
        abandoned = True
        try:
            group = control.abandon_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                lost=False,
                detail=failure_detail,
                idle_timeout_seconds=idle_timeout_s,
            )
        except Exception as abandon_exc:
            _log_control_failure("sandbox_abandon", job.job_id, abandon_exc)
            return 2
        if isinstance(group, dict) and group.get("state") == "lost":
            report_failure()
            return 2
        return 0 if control.release_turn(exc.reason) else 2
    except (DriverError, SandboxError, SessionSupervisorError, WorktreeError) as exc:
        failure_detail = str(exc)
        _log_control_failure(stage, job.job_id, exc)
        abandon_before_finish(lost=turn_admitted or context_invalid, detail=failure_detail)
        report_failure()
        return 2
    except Exception as exc:
        failure_detail = f"shared live chat failed unexpectedly: {_owner_visible_error(exc)}"
        _log_control_failure(stage, job.job_id, exc)
        abandon_before_finish(lost=turn_admitted, detail=failure_detail)
        report_failure()
        return 2
    finally:
        # Any path that stopped before the prompt handoff still reports one
        # record naming the stage it stopped at, instead of a silently missing
        # sample. After a successful dispatch this is a no-op.
        operation_profiling.finish(
            measurement,
            outcome="failed" if failure_detail else "incomplete",
            failure_stage=stage,
        )
        if not settled and not abandoned:
            abandon_before_finish(lost=turn_admitted or context_invalid, detail=failure_detail)
        if source is not None:
            source.discard()
        if supervisor is not None:
            supervisor.close()
        if terminals_suspended:
            with contextlib.suppress(Exception):
                control.resume_terminals()


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
