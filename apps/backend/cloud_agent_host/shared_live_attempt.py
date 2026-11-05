"""One attempt of a turn in its shared-live sandbox group.

The group is first checked and reserved with the control plane
(:mod:`cloud_agent_host.group_reservation`). The attempt then runs as a fixed
sequence of stages: it acquires the group from the sandbox manager, prepares
the workspace, starts or reuses the member's resident Agent CLI, admits and
streams the prompt, and settles both sides.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NamedTuple

from cloud_agent_host.attempt_lease import (
    Heartbeater,
    _retry_control_receipt,
    _retry_lifecycle_receipt,
)
from cloud_agent_host.chat_sandbox import (
    CHAT_UPLOADS_DIR,
    CHAT_WORKDIR,
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
from cloud_agent_host.group_reservation import (
    GroupPlan,
    GroupRefused,
    owner_visible_error,
    plan_group,
    reserve_group,
)
from cloud_agent_host.local_settings import _chat_idle_timeout_s
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import DELIVERY_HANDOFF_DRAIN_S
from cloud_agent_host.measurement_sender import _enqueue_delivery
from cloud_agent_host.resident_probe import probe_live_resident
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.resources import shared_member_resource_rollup
from cloud_agent_host.runtimes import AgentRuntime, NormalizedEvent
from cloud_agent_host.sandbox import SandboxBackend, SandboxError
from cloud_agent_host.sandbox_manager_client import (
    SandboxManagerClient,
    SandboxManagerError,
    sandbox_manager_from_env,
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

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cloud_agent_shared.environment import BoundEnvironment
    from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


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


class _TurnOutcome(NamedTuple):
    """What the admitted turn's stream ended with (:func:`_stream_shared_agent_turn`)."""

    exit_code: int
    tail: str
    tool_errors: list[str]
    result_frame: dict[str, Any] | None
    reported_failure: str | None

    @property
    def settled_failure(self) -> bool:
        """A failed turn whose native history and process the sandbox manager kept intact.

        A terminal native API error can leave its history and process intact.
        Only a sandbox manager result emitted after activation cleanup proves
        this boundary; process death, timeout and lost streams remain LOST.
        """
        return (
            self.exit_code != 0
            and isinstance(self.result_frame, dict)
            and self.result_frame.get("kind") == "result"
            and self.result_frame.get("status") == "failed"
            and self.result_frame.get("native_turn_settled") is True
        )


def _run_shared_live_attempt(
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
    try:
        plan = plan_group(job, backend=backend, host=host, settings=settings)
        reservation = reserve_group(plan, control=control, host=host, measurement=measurement)
        attempt = _SharedLiveAttempt(
            plan,
            reservation,
            runtime=runtime,
            control=control,
            heart=heart,
            agent_timeout_s=agent_timeout_s,
            finish_attempt=finish_attempt,
            measurement=measurement,
            settings=settings,
        )
    except GroupRefused as refused:
        finish_attempt("failed", str(refused))
        return 2
    except LeaseLost:
        return 3
    return attempt.run()


class _SharedLiveAttempt:
    """One reserved attempt, run as a sequence of stages (:meth:`run`).

    Each stage records itself in :attr:`stage` for failure reporting, and
    returns the attempt's exit code when the attempt ends there, or ``None``
    to go on. Whatever ends it, :meth:`run` releases or settles the group
    exactly once and reports the measurement.
    """

    # Set by the stages, each before any later stage or cleanup reads it.
    lease: dict[str, Any]
    acquire_started: float
    from_snapshot: bool
    container_action: str
    running: dict[str, Any]
    retained: RetainedChatBackend
    base_sha: str
    reused_workspace: bool
    launch: ResidentLaunch
    stage: str
    idle_timeout_s: float

    def __init__(
        self,
        plan: GroupPlan,
        reservation: dict[str, Any],
        *,
        runtime: AgentRuntime,
        control: ControlPlane,
        heart: Heartbeater,
        agent_timeout_s: float,
        finish_attempt: Any,
        measurement: operation_profiling.Measurement | None,
        settings: Mapping[str, float],
    ) -> None:
        job = plan.job
        seed_spec = plan.seed_spec
        self.job = job
        self.runtime = runtime
        self.control = control
        self.heart = heart
        self.agent_timeout_s = agent_timeout_s
        self.finish_attempt = finish_attempt
        self.measurement = measurement
        self.settings = settings
        self.mcp_url = plan.mcp_url
        self.manager = plan.manager
        self.reservation = reservation
        self.spec = ChatSandboxSpec(
            thread_id=str(reservation.get("origin_thread_id") or plan.origin_thread_id),
            sandbox_id=str(reservation["sandbox_id"]),
            generation=int(reservation["generation"]),
            runtime_id=str(reservation["runtime_id"]),
            runtime_version=reservation.get("runtime_version"),
            sandbox_image_digest=reservation.get("sandbox_image_digest"),
            shared_live=True,
            resident_slot_capacity=int(reservation.get("resident_slot_capacity") or plan.capacity),
            memory_limit=str(reservation.get("memory_limit") or seed_spec.memory_limit),
            cpu_limit=str(reservation.get("cpu_limit") or seed_spec.cpu_limit),
            disk_limit_mib=int(reservation.get("disk_limit_mib", seed_spec.disk_limit_mib)),
            stop_timeout_s=int(reservation.get("stop_timeout_seconds") or seed_spec.stop_timeout_s),
        )
        self.activation_generation = int(reservation["activation_generation"])
        # A group the control plane moved here is recreated from its checkpoint,
        # never created empty: the sandbox manager refuses a moved group without it.
        restore = reservation.get("restore") if reservation.get("state") == "restoring" else None
        if reservation.get("state") == "restoring" and not isinstance(restore, dict):
            raise GroupRefused("the control plane moved this chat without its checkpoint")
        self.restore = restore
        self.has_context = bool(reservation.get("last_settled_job_id"))
        self.native_session_id = str(reservation.get("native_session_id") or "").strip() or None
        self.manifest = _shared_group_manifest(reservation, thread_id=job.thread_id)
        self.supervisor: SandboxManagerClient | None = None
        self.source: GitSourcePrefetch | None = None
        self.lease_token: str | None = None
        self.snapshot: EnvironmentSnapshotRef | None = None
        self.snapshot_outcome: str | None = None
        self.settled = False
        # The sandbox manager has settled the turn: its session is good whatever
        # the control plane's receipt says next, and no longer leased to this attempt.
        self.locally_settled = False
        self.abandoned = False
        self.turn_admitted = False
        self.terminals_suspended = False
        self.context_invalid = False
        self.failure_detail: str | None = None
        self.imported_base: str | None = None
        self.recovered_initial = False

    def run(self) -> int:
        """Run every stage; whatever ends the attempt leaves the group released or settled."""
        self.stage = _stage(self.measurement, "supervisor_acquire")
        self.context_status = "restored" if self.has_context else "new"
        self.idle_timeout_s = float(
            self.settings.get("chat_idle_timeout_s") or _chat_idle_timeout_s()
        )
        try:
            return self._run_stages()
        except LeaseLost:
            self.failure_detail = "attempt lease lost during shared live execution"
            operation_profiling.finish(
                self.measurement, outcome="lease_lost", failure_stage=self.stage
            )
            return 3
        except _HandBack as exc:
            return self._hand_back(exc)
        except (DriverError, SandboxError, SandboxManagerError, WorktreeError) as exc:
            self.failure_detail = str(exc)
            _log_control_failure(self.stage, self.job.job_id, exc)
            self._abandon_before_finish(
                lost=self.turn_admitted or self.context_invalid, detail=self.failure_detail
            )
            self._report_failure()
            return 2
        except Exception as exc:
            self.failure_detail = (
                f"shared live chat failed unexpectedly: {owner_visible_error(exc)}"
            )
            _log_control_failure(self.stage, self.job.job_id, exc)
            self._abandon_before_finish(lost=self.turn_admitted, detail=self.failure_detail)
            self._report_failure()
            return 2
        finally:
            self._close()

    def _run_stages(self) -> int:
        steps: tuple[Callable[[], int | None], ...] = (
            self._acquire_group,
            self._mark_running,
            self._prepare_workspace,
            self._import_thread_uploads,
            self._record_initial_archive,
            self._preflight_runtime,
            self._prepare_agent,
            self._start_resident,
            self._admit_prompt,
        )
        for step in steps:
            exit_code = step()
            if exit_code is not None:
                return exit_code
        outcome = self._stream_turn()
        exit_code = self._end_unsettled_turn(outcome)
        if exit_code is not None:
            return exit_code
        return self._settle(outcome)

    def _acquired(self) -> tuple[SandboxManagerClient, str]:
        """The sandbox manager's client and this activation's lease, once acquired."""
        assert self.supervisor is not None and self.lease_token is not None
        return self.supervisor, self.lease_token

    # ── stages, in order ───────────────────────────────────────────────

    def _acquire_group(self) -> None:
        """Start preparing a new chat's source, then acquire the group from the sandbox manager."""
        job, spec = self.job, self.spec
        if not self.has_context:
            # Clone into a host-local temporary archive while the activation
            # below creates and boots the guest. This cannot mutate the
            # sandbox whose durable initial marker still has to be probed. If
            # that probe recovers a prepared baseline, discard this speculative
            # source instead of importing it.
            import_mib = self.settings.get("source_import_max_mib")
            self.source = _prefetch_retained_source(
                job=job,
                uid=self.manager.backend.uid,
                gid=self.manager.backend.gid,
                max_bytes=None if import_mib is None else int(import_mib) * 1024 * 1024,
            )
        self.snapshot, self.snapshot_outcome = _choose_environment_snapshot(
            job,
            spec,
            self.source,
            new_group=not self.has_context
            and self.restore is None
            and not self.reservation.get("container_id"),
        )
        self.supervisor = supervisor = sandbox_manager_from_env()
        try:
            acquire_call = operation_profiling.expect_fragment(
                self.measurement, SANDBOX_MANAGER_PRODUCER, "supervisor_acquire"
            )
            # Before the call, so a proof the sandbox manager makes during it is never
            # taken for younger than it is.
            self.acquire_started = time.monotonic()
            lease = supervisor.acquire_shared(
                spec,
                thread_id=job.thread_id,
                activation_generation=self.activation_generation,
                owner=f"attempt:{job.attempt_id}",
                has_settled_context=self.has_context,
                members=self.manifest,
                expected_container_id=self.reservation.get("container_id"),
                profiling=operation_profiling.measurement_context(self.measurement, acquire_call),
                start_seq=self.reservation.get("start_seq"),
                restore=self.restore,
                environment_snapshot=(
                    self.snapshot.model_dump(mode="json") if self.snapshot is not None else None
                ),
            )
        except SandboxManagerError as exc:
            if self.restore is not None and exc.status_code == 424:
                raise _HandBack(
                    RESTORE_FAILED_RELEASE_REASON, f"sandbox restore failed: {exc}"
                ) from exc
            if exc.status_code == 409:
                # The group stayed busy past the sandbox manager's wait: a
                # snapshot capture can take longer, and the control plane
                # claims a follow-up whatever the group is doing.
                raise _HandBack("sandbox_busy", f"sandbox busy: {exc}") from exc
            self.context_invalid = self.has_context and exc.status_code == 410
            raise
        self.lease = lease
        self.lease_token = str(lease["token"])
        self._accept_lease(lease, acquire_call)

    def _accept_lease(self, lease: dict[str, Any], acquire_call: str | None) -> None:
        # The container holds a snapshot's installation, whether this attempt
        # or an earlier one created it, and its workspace must be kept.
        self.from_snapshot = bool(lease.get("environment_snapshot_id"))
        if self.snapshot is not None:
            self.snapshot_outcome = (
                "used"
                if lease.get("environment_snapshot_id") == self.snapshot.snapshot_id
                else str(lease.get("environment_snapshot_failure") or "unavailable")
            )
        self.container_action = str(lease.get("container_action") or "unknown")
        # One answer for one call. Sub-stages the sandbox manager measured are durations,
        # not offsets, so they hang off the stage they happened in rather than
        # being placed on this process's timeline.
        operation_profiling.record_fragment(
            self.measurement,
            producer=SANDBOX_MANAGER_PRODUCER,
            call_id=acquire_call,
            stage="supervisor_acquire",
            response=lease,
        )
        if int(lease["activation_generation"]) != self.activation_generation:
            raise SandboxError("shared supervisor returned the wrong activation generation")
        if self.reservation.get("container_id") not in {None, lease.get("container_id")}:
            raise SandboxError("shared supervisor returned a replacement chat container")
        self.context_status = str(lease.get("context_status") or self.context_status)
        if self.context_status not in {"new", "live", "restored"}:
            raise SandboxError(
                f"shared supervisor returned invalid context {self.context_status!r}"
            )

    def _mark_running(self) -> int | None:
        """Record the running group, announce the turn, and hold the workspace's terminals."""
        job, lease = self.job, self.lease
        self.stage = _stage(self.measurement, "sandbox_running")
        self.running = self.control.mark_chat_sandbox_running(
            sandbox_generation=self.spec.generation,
            activation_generation=self.activation_generation,
            container_id=str(lease["container_id"]),
            context_status=self.context_status,
        )
        # The sandbox manager proved the group bridge inside that call, the same check
        # the turn's commands would repeat. A sandbox manager that does not say so leaves
        # every command to check for itself, as before.
        self.retained = RetainedChatBackend(
            self.manager,
            self.spec,
            network_proved_at=(
                self.acquire_started if lease.get("network_verified") is True else None
            ),
        )
        self.control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "started",
                    "runtime": job.runtime,
                    "attempt_no": job.attempt_no,
                    "context_status": self.context_status,
                    "resident_state": lease.get("resident_state"),
                    "workspace_shared": True,
                    "container_id": lease["container_id"],
                    "activation_generation": self.activation_generation,
                    **{**self.retained.sandbox_metadata(), **job.started_context_fields()},
                },
            )
        )
        if self._stopped_before_admission(None):
            return 0
        self.stage = _stage(self.measurement, "terminal_suspend")
        self.control.suspend_terminals("workspace_preparing")
        self.terminals_suspended = True
        return None

    def _prepare_workspace(self) -> int | None:
        """Check out a new chat's source, or bring a follow-up's workspace to its base."""
        if not self.has_context and self.container_action != "create":
            self._resolve_initial_archive()
        if self.has_context:
            self._resume_workspace()
            return None
        if not self.recovered_initial:
            # The marker probe proved there is no prepared baseline. Only now
            # may the prefetched source mutate the sandbox.
            return self._prepare_new_workspace()
        return None

    def _resolve_initial_archive(self) -> None:
        # A previous activation may have committed the immutable S3
        # baseline and then died before returning its receipt. Resolve
        # that durable marker before reset, setup, permissions, or uploads
        # can alter the exact prepared bytes it names. A container this
        # acquire created holds no marker it wrote, so none to resolve: the
        # capture clears one a checkpoint carried and refuses any other.
        supervisor, token = self._acquired()
        self.stage = _stage(self.measurement, "initial_archive_status")
        initial_status = supervisor.initial_archive_status(
            self.spec,
            thread_id=self.job.thread_id,
            token=token,
            members=self.manifest,
            expected_container_id=str(self.lease["container_id"]),
            preparation_id=self.job.job_id,
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
                self.base_sha = recovered_base
                self.recovered_initial = True
                self.reused_workspace = True
                valid_status = True
        if not valid_status:
            raise SandboxError("sandbox manager returned an invalid initial archive status")
        if self.recovered_initial and self.source is not None:
            self.source.discard()
            self.source = None

    def _prepare_new_workspace(self) -> int | None:
        job, spec = self.job, self.spec
        self.stage = _stage(self.measurement, "workspace_reset")
        self.manager.reset_unsettled_state(spec, keep_workspace=self.from_snapshot)
        self.stage = _stage(self.measurement, "checkout")
        if self.source is None:
            raise SandboxError("a new chat's source was never prepared")
        self.base_sha = self.source.import_into(
            self.manager, spec, observer=operation_profiling.stage_observer(self.measurement)
        )
        if self.snapshot_outcome is not None:
            self.control.report_environment_snapshot(
                snapshot_id=(
                    None
                    if self.snapshot_outcome == "absent" or job.environment_snapshot is None
                    else job.environment_snapshot.snapshot_id
                ),
                outcome=self.snapshot_outcome,
                base_sha=self.base_sha,
            )
        self.reused_workspace = False
        if job.setup_script and job.environment is None:
            exit_code = self._run_setup_script(job.setup_script)
            if exit_code is not None:
                return exit_code
        self.stage = _stage(self.measurement, "workspace_permissions")
        self.manager.prepare_shared_workspace(spec)
        return None

    def _run_setup_script(self, script: str) -> int | None:
        self.stage = _stage(self.measurement, "setup")
        if self._stopped_before_admission(self.base_sha):
            return 0
        setup = run_setup(
            script=script,
            workdir=CHAT_WORKDIR,
            backend=self.retained,
            stop=lambda: self.heart.cancel_requested or self.heart.lease_lost,
        )
        self.control.append_event(
            NormalizedEvent(
                "lifecycle",
                {"phase": "setup", "ran": setup.ran},
            )
        )
        if self._stopped_before_admission(self.base_sha):
            return 0
        if setup.exit_code != 0:
            self.failure_detail = f"setup failed ({setup.exit_code}): {setup.detail}"
            self._abandon_before_finish(lost=False, detail=self.failure_detail)
            self.finish_attempt("failed", self.failure_detail, base_sha=self.base_sha)
            return 1
        return None

    def _resume_workspace(self) -> None:
        job = self.job
        self.stage = _stage(self.measurement, "native_workspace_resume")
        if not self.native_session_id:
            self.context_invalid = True
            raise SandboxError("shared chat has no native Agent CLI session locator")
        if not _resident_base_ready(CHAT_WORKDIR, job, self.retained):
            self.imported_base = _import_retained_base(
                job=job, manager=self.manager, spec=self.spec, backend=self.retained
            )
            if not _resident_base_ready(CHAT_WORKDIR, job, self.retained):
                self.context_invalid = True
                raise SandboxError("shared chat cannot resolve its pinned base commit")
            self.control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "base_imported",
                        "base_sha": self.imported_base,
                        "workspace_head_preserved": True,
                    },
                )
            )
        self.base_sha = job.base_sha or _retained_head(self.retained)
        self.reused_workspace = True

    def _import_thread_uploads(self) -> None:
        """Every turn, after either workspace path, and before the turn is admitted.

        A first turn populates a new container and a follow-up re-populates
        one that may have been rebuilt, so the agent never observes a
        half-written directory.
        """
        self.stage = "uploads"
        imported_uploads = _import_uploads(
            job=self.job,
            control=self.control,
            manager=self.manager,
            spec=self.spec,
            # A recovered baseline belongs to the same retained container, so
            # its earlier thread uploads are already present. A replacement
            # first-turn job can still add files of its own and must import the
            # thread's current attachment set before prompt admission.
            fresh_container=not self.has_context and not self.recovered_initial,
            container_id=str(self.lease["container_id"]),
        )
        if imported_uploads:
            self.control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "uploads_imported",
                        "files": imported_uploads,
                        "path": CHAT_UPLOADS_DIR,
                    },
                )
            )

    def _record_initial_archive(self) -> None:
        """Capture a new chat's prepared workspace, then report the checkout."""
        if not self.has_context and not self.recovered_initial:
            self._capture_initial_archive()
        elif self.recovered_initial:
            self.control.append_event(
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
        self.control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "checked_out",
                    "base_sha": self.base_sha,
                    "reused": self.reused_workspace,
                    "storage": "docker_writable_layer",
                    "workspace_shared": True,
                    # Nothing was cloned: the tree is the one empty commit, and
                    # the owner is told so instead of "repository ready".
                    **({"temporary": True} if self.job.repo is None else {}),
                },
            )
        )

    def _capture_initial_archive(self) -> None:
        # Source, setup, permissions, and uploaded files are complete, and
        # no resident CLI or prompt exists yet. Capture this point-in-time
        # boundary before admitting either. The sandbox manager owns S3 access; the
        # runner never receives cloud credentials.
        supervisor, token = self._acquired()
        self.stage = _stage(self.measurement, "initial_archive")
        initial_archive = supervisor.capture_initial_archive(
            self.spec,
            thread_id=self.job.thread_id,
            token=token,
            members=self.manifest,
            expected_container_id=str(self.lease["container_id"]),
            preparation_id=self.job.job_id,
            base_sha=self.base_sha,
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
        self.control.append_event(
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

    def _preflight_runtime(self) -> int | None:
        """Refuse a runtime version or binary the group cannot run."""
        self.stage = _stage(self.measurement, "runtime_preflight")
        version_problem = runtime_version_unavailable(self.job, self.retained)
        if version_problem is not None:
            self.failure_detail = version_problem
            self._abandon_before_finish(lost=False, detail=self.failure_detail)
            self.finish_attempt("failed", self.failure_detail, base_sha=self.base_sha)
            return 1
        if self.lease.get("resident_state") != "live" and not self.retained.has_binary(
            self.runtime.binary
        ):
            self.failure_detail = f"runtime binary {self.runtime.binary!r} is not installed"
            self._abandon_before_finish(lost=False, detail=self.failure_detail)
            self.finish_attempt("failed", self.failure_detail, base_sha=self.base_sha)
            return 2
        return None

    def _prepare_agent(self) -> int | None:
        """Prove the bridge, grant SSH access and prepare the environment, then free terminals."""
        job = self.job
        self.stage = _stage(self.measurement, "agent_network")
        # This manager runs in the runner's own process, so its sub-stages are
        # timed directly onto the measurement. Setup ran on this same group
        # bridge; nothing is switched here, only proved still in place before
        # the CLI starts, which the activation's proof already does unless
        # setup ran past a minute.
        network_observer = operation_profiling.stage_observer(self.measurement)
        self.retained.require_network(observer=network_observer)
        if job.ssh_access is not None:
            # Before setup and the agent: this member holds the keys of its
            # selected servers, which _claim_secret_values already masks, and
            # the bridge reaches them. A newer state the control plane already
            # pushed wins: what it closed stays closed.
            supervisor, _token = self._acquired()
            ssh_slot = int(
                self.running.get("resident_slot_id")
                or self.reservation.get("resident_slot_id")
                or 0
            )
            supervisor.sync_ssh_access(
                job.thread_id, job.ssh_access.with_slot(job.thread_id, ssh_slot), starting=True
            )
        if job.environment is not None:
            exit_code = self._prepare_environment(job.environment)
            if exit_code is not None:
                return exit_code
        self.stage = _stage(self.measurement, "terminal_resume")
        self.control.resume_terminals()
        self.terminals_suspended = False
        return None

    def _prepare_environment(self, environment: BoundEnvironment) -> int | None:
        supervisor, token = self._acquired()
        try:
            prepared = supervisor.prepare_environment(
                self.job.thread_id,
                environment,
                activation_owner=token,
                cancelled=lambda: self.heart.cancel_requested or self.heart.lease_lost,
            )
        except SandboxManagerError:
            # A Stop kills the running script, and the preparation then
            # fails. Nothing has reached the CLI yet, so it is no failure.
            if self._stopped_before_admission(self.base_sha):
                return 0
            raise
        self.control.append_event(
            NormalizedEvent("lifecycle", {"phase": "environment_prepared", **prepared})
        )
        return None

    def _start_resident(self) -> None:
        """Borrow the member's live CLI process, or open a residency and start one."""
        supervisor, token = self._acquired()
        launch = self._resident_launch()
        self.stage = _stage(self.measurement, "resident_start")
        resident_descriptor, resident_call = probe_live_resident(
            self.measurement,
            supervisor,
            lease=self.lease,
            thread_id=self.job.thread_id,
            token=token,
            launch=launch,
        )
        if resident_descriptor is None:
            launch, resident_descriptor, resident_call = self._open_resident(launch)
        self.launch = launch
        self._record_resident(resident_descriptor, resident_call)

    def _resident_launch(self) -> ResidentLaunch:
        job = self.job
        slot_id = int(
            self.running.get("resident_slot_id") or self.reservation.get("resident_slot_id") or 0
        )
        return ResidentLaunch(
            thread_id=job.thread_id,
            runtime_id=job.runtime,
            runtime_version=job.runtime_version,
            native_session_id=self.native_session_id,
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
            mcp_base_url=self.mcp_url if job.mcp_bindings else "",
            mcp_credential="",
            sandbox_mcp_servers=job.sandbox_mcp_servers if job.mcp_bindings else (),
            slot_id=slot_id,
            residency_generation=int(self.running.get("residency_generation") or 0),
            **model_connection_launch(job),
        )

    def _open_resident(
        self, launch: ResidentLaunch
    ) -> tuple[ResidentLaunch, dict[str, Any], str | None]:
        """Open a residency for a new CLI process and start it under that residency."""
        job = self.job
        supervisor, token = self._acquired()
        residency = self.control.establish_shared_residency(
            sandbox_id=self.spec.sandbox_id,
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
            self.measurement, SANDBOX_MANAGER_PRODUCER, "resident_start"
        )
        resident_descriptor = supervisor.ensure_shared_resident(
            job.thread_id,
            token=token,
            launch=launch,
            profiling=operation_profiling.measurement_context(self.measurement, resident_call),
        )
        return launch, resident_descriptor, resident_call

    def _record_resident(
        self, resident_descriptor: dict[str, Any], resident_call: str | None
    ) -> None:
        self.context_status = str(resident_descriptor.get("context_status") or self.context_status)
        if self.context_status not in {"new", "live", "restored"}:
            raise SandboxError(
                f"shared supervisor returned invalid resident context {self.context_status!r}"
            )
        process_generation = int(resident_descriptor["process_generation"])
        resident_action = str(resident_descriptor.get("resident_action") or "unknown")
        operation_profiling.record_fragment(
            self.measurement,
            producer=SANDBOX_MANAGER_PRODUCER,
            call_id=resident_call,
            stage="resident_start",
            response=resident_descriptor,
        )
        if self.measurement is not None:
            operation_profiling.set_attributes(
                self.measurement,
                context_status=self.context_status,
                container_action=self.container_action,
                resident_action=resident_action,
                startup_path=_classify_startup_path(self.container_action, resident_action),
                process_generation=process_generation,
                container_id=str(self.lease["container_id"]),
                workspace_action=(
                    "prepare"
                    if not self.has_context
                    else ("base_import" if self.imported_base else "reuse")
                ),
            )
        continuity: tuple[str | None, str] = (None, "new")
        if self.context_status == "live":
            continuity = ("hot", "live")
        elif self.has_context:
            continuity = ("warm_native", "restored")
        self.control.record_chat_process(
            sandbox_generation=self.spec.generation,
            activation_generation=self.activation_generation,
            residency_generation=self.launch.residency_generation,
            process_generation=process_generation,
            continuity=continuity,
        )

    def _admit_prompt(self) -> int | None:
        """Admit the turn with the control plane: the last point at which a Stop costs nothing."""
        self.stage = _stage(self.measurement, "prompt_admission")
        if self._stopped_before_admission(self.base_sha):
            return 0
        _retry_control_receipt(
            "prompt_admission_retry",
            self.job.job_id,
            lambda: self.control.admit_chat_turn(
                sandbox_generation=self.spec.generation,
                activation_generation=self.activation_generation,
                residency_generation=self.launch.residency_generation,
            ),
            heart=self.heart,
        )
        self.turn_admitted = True
        return None

    def _stream_turn(self) -> _TurnOutcome:
        """Hand the prompt over and relay the turn, then report the resources it used."""
        supervisor, token = self._acquired()
        job, control = self.job, self.control
        result_frame: dict[str, Any] | None = None
        try:
            self.stage = "turn_stream"
            # The prompt handoff is the last startup stage. `stage` stays a
            # plain string because a failure inside the turn is an execution
            # failure, not a startup stage.
            operation_profiling.mark(self.measurement, "prompt_dispatch")

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
                    heart=self.heart,
                    lease_token=token,
                    timeout_s=self.agent_timeout_s,
                    measurement=self.measurement,
                    on_frozen=_frozen_measurement,
                )
            )
        finally:
            self._report_turn_resources(result_frame)
        return _TurnOutcome(exit_code, tail, tool_errors, result_frame, reported_failure)

    def _report_turn_resources(self, result_frame: dict[str, Any] | None) -> None:
        # Container totals include concurrent siblings and cannot be
        # attributed to this member. The sandbox manager instead returns UID-scoped
        # guest process telemetry and per-turn tool windows; if the stream
        # died before a terminal frame, retain an explicit zero-coverage
        # row rather than inventing a share of the container total.
        with contextlib.suppress(Exception):
            fallback = shared_member_resource_rollup()
            observed = result_frame.get("resources") if isinstance(result_frame, dict) else None
            self.control.report_resources(observed if isinstance(observed, dict) else fallback)
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
                self.control.report_turn_resources(reportable)

    def _end_unsettled_turn(self, outcome: _TurnOutcome) -> int | None:
        """End a turn the sandbox manager did not settle; ``None`` for one it did."""
        if outcome.exit_code == 130:
            self.failure_detail = "cancelled by owner after shared-session admission"
            self._abandon_before_finish(lost=True, detail=self.failure_detail)
            self._finish_decided("cancelled", "cancelled by owner", self.base_sha)
            return 0
        if outcome.exit_code == 124:
            self.failure_detail = "shared Agent CLI turn exceeded its execution timeout"
            self._abandon_before_finish(lost=True, detail=self.failure_detail)
            self.finish_attempt("failed", self.failure_detail, base_sha=self.base_sha)
            return 1
        if outcome.exit_code != 0 and not outcome.settled_failure:
            self.failure_detail = (
                outcome.reported_failure
                or f"agent exited {outcome.exit_code}: {outcome.tail[-500:]}"
            )
            self._abandon_before_finish(lost=True, detail=self.failure_detail)
            self.finish_attempt("failed", self.failure_detail, base_sha=self.base_sha)
            return 1
        if outcome.tool_errors:
            # Reported, not failed: a successful shared turn may race a
            # sibling, so tool errors alone do not prove this member produced
            # no useful state.
            self.control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {"phase": "shared_tool_errors_observed", "count": len(outcome.tool_errors)},
                )
            )
        return None

    def _settle(self, outcome: _TurnOutcome) -> int:
        """Settle the turn with the sandbox manager, then the control plane, then finish."""
        supervisor, token = self._acquired()
        job = self.job
        self.stage = _stage(self.measurement, "settlement")
        local_settle = _retry_lifecycle_receipt(
            "supervisor_settlement_retry",
            job.job_id,
            lambda: supervisor.settle_shared(
                job.thread_id,
                token=token,
                job_id=job.job_id,
                context_status=self.context_status,
            ),
        )
        settled_session_id = _verified_native_session_id(
            previous_session_id=self.native_session_id,
            observed_session_id=local_settle.get("native_session_id"),
            has_context=self.has_context,
        )
        self.locally_settled = True
        _retry_control_receipt(
            "control_settlement_retry",
            job.job_id,
            lambda: self.control.settle_chat_sandbox(
                sandbox_generation=self.spec.generation,
                activation_generation=self.activation_generation,
                residency_generation=self.launch.residency_generation,
                context_status=self.context_status,
                native_session_id=settled_session_id,
                base_sha=self.base_sha,
                idle_timeout_seconds=self.idle_timeout_s,
            ),
            heart=self.heart,
        )
        self.settled = True
        if outcome.settled_failure:
            # Record the native turn ordinal before failing the job, so later
            # retries and historical forks keep matching the CLI transcript.
            self._finish_decided(
                "failed",
                outcome.reported_failure
                or f"agent failed at native turn boundary: {outcome.tail[-500:]}",
                self.base_sha,
            )
            return 1
        try:
            self.control.append_event(
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
        self._finish_decided(
            "succeeded",
            "agent completed (shared workspace)",
            self.base_sha,
        )
        return 0

    # ── ending the attempt ─────────────────────────────────────────────

    def _hand_back(self, exc: _HandBack) -> int:
        # Nothing ran and the chat is unchanged: release the activation, which
        # gives a group being restored back to its checkpoint, and the turn
        # back to the queue, for this host or another to try again once the
        # control plane's retry delay has passed. Once the control plane has
        # run out of restore attempts it marks the group lost instead, and this
        # turn fails with it.
        self.failure_detail = str(exc)
        _log_control_failure(self.stage, self.job.job_id, exc)
        self.abandoned = True
        try:
            group = self.control.abandon_chat_sandbox(
                sandbox_generation=self.spec.generation,
                activation_generation=self.activation_generation,
                lost=False,
                detail=self.failure_detail,
                idle_timeout_seconds=self.idle_timeout_s,
            )
        except Exception as abandon_exc:
            _log_control_failure("sandbox_abandon", self.job.job_id, abandon_exc)
            return 2
        if isinstance(group, dict) and group.get("state") == "lost":
            self._report_failure()
            return 2
        return 0 if self.control.release_turn(exc.reason) else 2

    def _close(self) -> None:
        # Any path that stopped before the prompt handoff still reports one
        # record naming the stage it stopped at, instead of a silently missing
        # sample. After a successful dispatch this is a no-op.
        operation_profiling.finish(
            self.measurement,
            outcome="failed" if self.failure_detail else "incomplete",
            failure_stage=self.stage,
        )
        if not self.settled and not self.abandoned:
            self._abandon_before_finish(
                lost=self.turn_admitted or self.context_invalid, detail=self.failure_detail
            )
        if self.source is not None:
            self.source.discard()
        if self.supervisor is not None:
            self.supervisor.close()
        if self.terminals_suspended:
            with contextlib.suppress(Exception):
                self.control.resume_terminals()

    def _abandon_before_finish(self, *, lost: bool, detail: str | None) -> None:
        if self.abandoned or self.settled:
            return
        if (
            self.supervisor is not None
            and self.lease_token is not None
            and not self.locally_settled
        ):
            try:
                self.supervisor.abandon_shared(
                    self.job.thread_id, token=self.lease_token, lost=lost
                )
            except Exception as exc:
                _log_control_failure("supervisor_abandon", self.job.job_id, exc)
        try:
            self.control.abandon_chat_sandbox(
                sandbox_generation=self.spec.generation,
                activation_generation=self.activation_generation,
                lost=lost,
                detail=detail,
                idle_timeout_seconds=self.idle_timeout_s,
            )
        except Exception as exc:
            _log_control_failure("sandbox_abandon", self.job.job_id, exc)
        self.abandoned = True

    def _report_failure(self) -> None:
        # Telemetry availability must never gate the authoritative transition.
        # Keep trying finish even when the error-event endpoint is unavailable.
        detail = (self.failure_detail or "shared live execution failed")[:1000]
        try:
            self.control.append_event(NormalizedEvent("error", {"text": detail}))
        except Exception as exc:
            _log_control_failure("error_event", self.job.job_id, exc)
        try:
            self.finish_attempt("failed", detail)
        except Exception as exc:
            _log_control_failure("finish", self.job.job_id, exc)

    def _finish_decided(self, state: str, detail: str, base_sha: str | None) -> None:
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
                self.job.job_id,
                lambda: self.finish_attempt(state, detail, base_sha=base_sha),
            )
        except LeaseLost:
            raise
        except Exception as exc:
            _log_control_failure("finish", self.job.job_id, exc)

    def _stopped_before_admission(self, base_sha: str | None) -> bool:
        # Nothing has reached the CLI yet, so a Stop costs nothing here: the
        # activation is released with the conversation intact and the job is
        # finished cancelled. Admitting anyway would spend the prompt only to
        # interrupt it, and an interrupted turn leaves its native context
        # lost. The heartbeat may carry the Stop, or a lost lease, at any time
        # while the turn starts, so this is asked after the container starts,
        # around the setup script, and once more right before admission.
        if self.heart.lease_lost:
            raise LeaseLost("lease lost before prompt admission")
        if not self.heart.cancel_requested:
            return False
        self.failure_detail = "cancelled by owner before prompt admission"
        operation_profiling.finish(self.measurement, outcome="cancelled", failure_stage=self.stage)
        self._abandon_before_finish(lost=False, detail=self.failure_detail)
        self._finish_decided("cancelled", "cancelled by owner", base_sha)
        return True
