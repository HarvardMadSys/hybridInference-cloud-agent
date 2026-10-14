"""Concurrent member supervisor for one shared retained sandbox workspace."""

from __future__ import annotations

import contextlib
import logging
import math
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.chat_sandbox import (
    CHAT_CPU_LIMIT_LABEL,
    CHAT_DISK_LIMIT_LABEL,
    CHAT_GENERATION_LABEL,
    CHAT_IMAGE_DIGEST_LABEL,
    CHAT_MEMORY_LIMIT_LABEL,
    CHAT_RESIDENT_CAPACITY_LABEL,
    CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SHARED_LABEL,
    CHAT_STOP_TIMEOUT_LABEL,
    CHAT_THREAD_LABEL,
    MEMBER_UID_BASE,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatBackend,
    RetainedChatFailStopped,
    RetainedChatLost,
    SandboxActionFacts,
)
from cloud_agent_host.drivers import DialectRuntime, InputOutcome, PendingInput
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_SOURCE,
    GuestObservation,
    GuestObserverCursor,
)
from cloud_agent_host.member_resources import (
    MEMBER_WINDOW_SAMPLE_INTERVAL_MS,
    RUNNING_CHECKPOINT_INTERVAL_S,
    CheckpointSchedule,
    MemberWindowHandle,
    MemberWindowSampler,
    WindowAccumulator,
    reduce_window,
)
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransport,
    ResidentTransportFactory,
    ResidentTurn,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError
from cloud_agent_host.session_lifecycle import (
    DEFAULT_CHAT_IDLE_TIMEOUT_S,
    DEFAULT_SWEEP_INTERVAL_S,
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_shared.errors import SandboxArchiveBlocked
from cloud_agent_shared.operation_profiling import (
    StageRecorder,
    context_call_id,
    start_stage_recording,
    substage,
)
from cloud_agent_shared.shared_protocol import INPUT_TURN_ENDED
from cloud_agent_shared.turn_resources import parse_cpu_capacity

if TYPE_CHECKING:
    import builtins

    from cloud_agent_host.tool_windows import ToolMark, ToolMarkReader

ContextStatus = Literal["new", "live", "restored", "lost"]
ResidentState = Literal["stopped", "starting", "live", "restarting", "lost"]

#: One window the census loop is about to sample, as it leaves the lock: the
#: handle, its sampler, the census number, and the cursor and accumulator the
#: journal read needs. Named because the hand-off is what lets the guest exec
#: happen outside the lock, and a reader has to be able to see that the handle
#: and the sampler are already known to be present while the other two are not.
DueCensus = tuple[
    MemberWindowHandle,
    MemberWindowSampler,
    int,
    GuestObserverCursor | None,
    WindowAccumulator | None,
]

logger = logging.getLogger(__name__)

_ARCHIVE_FAILURE_DIAGNOSTICS_ATTR = "_cloud_agent_archive_failure_diagnostics"
_ARCHIVE_ERROR_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ArchiveFailurePhase = Literal["initial", "fail_stop_final"]


@dataclass(frozen=True)
class _ArchiveFailureDiagnostic:
    """One bounded archive error description safe to cross the broker API."""

    phase: _ArchiveFailurePhase
    error_type: str
    error_code: str | None = None
    http_status: int | None = None

    def render(self) -> str:
        error_type = self.error_type if _ARCHIVE_ERROR_TOKEN.fullmatch(self.error_type) else "Error"
        parts = [f"phase={self.phase}", f"type={error_type}"]
        if self.error_code is not None and _ARCHIVE_ERROR_TOKEN.fullmatch(self.error_code):
            parts.append(f"code={self.error_code}")
        if (
            self.http_status is not None
            and not isinstance(self.http_status, bool)
            and 100 <= self.http_status <= 599
        ):
            parts.append(f"status={self.http_status}")
        return f"archive failure ({', '.join(parts)})"


def _archive_failure_diagnostic(
    phase: _ArchiveFailurePhase,
    error: BaseException,
) -> _ArchiveFailureDiagnostic:
    """Classify an exception without copying any provider-controlled message."""
    current: BaseException | None = error
    selected = error
    error_code: str | None = None
    http_status: int | None = None
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        selected = current
        try:
            response = getattr(current, "response", None)
        except Exception:
            response = None
        if isinstance(response, Mapping):
            error_payload = response.get("Error")
            metadata = response.get("ResponseMetadata")
            candidate_code = (
                error_payload.get("Code") if isinstance(error_payload, Mapping) else None
            )
            candidate_status = (
                metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
            )
            if isinstance(candidate_code, str) and _ARCHIVE_ERROR_TOKEN.fullmatch(candidate_code):
                error_code = candidate_code
            if (
                isinstance(candidate_status, int)
                and not isinstance(candidate_status, bool)
                and 100 <= candidate_status <= 599
            ):
                http_status = candidate_status
            if error_code is not None or http_status is not None:
                break
        current = current.__cause__
        if current is None and not selected.__suppress_context__:
            current = selected.__context__
    error_type = type(selected).__name__
    if _ARCHIVE_ERROR_TOKEN.fullmatch(error_type) is None:
        error_type = "Error"
    return _ArchiveFailureDiagnostic(
        phase=phase,
        error_type=error_type,
        error_code=error_code,
        http_status=http_status,
    )


def _safe_error_type(error: BaseException) -> str:
    """Return only a bounded Python type name, never an exception message."""
    name = type(error).__name__
    return name if _ARCHIVE_ERROR_TOKEN.fullmatch(name) else "Error"


def _record_archive_failure_diagnostics(
    error: BaseException,
    *,
    initial: BaseException,
    fail_stop_final: BaseException | None = None,
) -> None:
    """Attach only safe fields before ``raise ... from`` replaces a cause chain."""
    diagnostics = [_archive_failure_diagnostic("initial", initial)]
    if fail_stop_final is not None:
        diagnostics.append(_archive_failure_diagnostic("fail_stop_final", fail_stop_final))
    setattr(error, _ARCHIVE_FAILURE_DIAGNOSTICS_ATTR, tuple(diagnostics))


def archive_failure_http_detail(error: BaseException) -> str | None:
    """Return a bounded broker response for a recorded initial archive failure."""
    diagnostics = getattr(error, _ARCHIVE_FAILURE_DIAGNOSTICS_ATTR, None)
    if not isinstance(diagnostics, tuple) or not diagnostics:
        return None
    if not all(isinstance(item, _ArchiveFailureDiagnostic) for item in diagnostics):
        return None
    return "; ".join(item.render() for item in diagnostics)


@dataclass(frozen=True)
class SharedMemberManifest:
    """Durable fields needed to reconstruct one group membership."""

    thread_id: str
    native_session_id: str | None = None
    context_status: ContextStatus = "new"
    activation_generation: int = 0
    process_generation: int = 0
    residency_generation: int = 0
    resident_state: ResidentState = "stopped"
    resident_slot_id: int | None = None
    last_settled_job_id: str | None = None


@dataclass(frozen=True)
class SharedSessionLease:
    """Receipt proving one caller owns one member activation, not the group."""

    thread_id: str
    origin_thread_id: str
    sandbox_id: str
    container_id: str
    container_name: str
    token: str
    activation_generation: int
    process_generation: int
    context_status: ContextStatus
    resident_state: ResidentState
    resident_slot_id: int
    #: What this activation actually did to the container: ``created``,
    #: ``adopted``, ``started``, ``reused`` or ``unknown``. The runner cannot
    #: derive it from the container's state afterwards, and the four startup
    #: paths differ precisely by this action.
    container_action: str = "unknown"
    #: Sub-stage durations measured in this process, if it is part of an
    #: experiment. Durations only: another process's monotonic offsets would
    #: not be comparable with the caller's.
    stages: list[dict[str, Any]] = field(default_factory=list)
    #: Which caller call this answers, echoed back so a retry is a second
    #: fragment rather than a replacement for the first.
    profiling_call_id: str | None = None
    #: Whether this process finished collecting for that call. Distinct from
    #: `stages` being empty: a reuse that legitimately did no timed work still
    #: collected, and reporting it as missing made every reuse incomplete.
    profiling_recorded: bool = False
    #: Whether that collection was cut short by this process's own deadline.
    #: An empty `stages` means "collected and nothing to time" only when this
    #: is false -- otherwise it means the stages were skipped and never
    #: measured, which must not pass for a complete fragment.
    profiling_truncated: bool = False
    #: Whether this call proved the group bridge, as ``ChatSandboxManager.start``
    #: does before answering. The runner's commands for the turn rely on that
    #: proof for a minute instead of repeating it. False when the same owner
    #: acquires again and is handed its existing lease untouched.
    network_verified: bool = False


@dataclass(frozen=True)
class SharedSessionDescriptor:
    """Docker group plus one member's independently fenced process state."""

    thread_id: str
    origin_thread_id: str
    sandbox_id: str
    generation: int
    container_id: str
    container_name: str
    docker_status: str
    running: bool
    group_busy: bool
    busy: bool
    terminal_count: int
    operation_count: int
    snapshotting: bool
    activation_generation: int
    process_generation: int
    residency_generation: int
    resident_state: ResidentState
    resident_slot_id: int
    last_settled_job_id: str | None
    native_session_id: str | None
    idle_deadline_monotonic: float | None
    context_status: ContextStatus
    #: What the resident call did to the member's CLI process: ``reuse`` kept
    #: the live process, ``resume`` reopened the native session, ``new``
    #: started a process with no session to resume.
    resident_action: str = "unknown"
    #: Sub-stage durations measured in this process, if it is part of an
    #: experiment. See :class:`SharedSessionLease`.
    stages: list[dict[str, Any]] = field(default_factory=list)
    #: See :class:`SharedSessionLease`.
    profiling_call_id: str | None = None
    profiling_recorded: bool = False
    profiling_truncated: bool = False


@dataclass(frozen=True)
class NativeSessionForkResult:
    """A prompt-free native child already backed by a live resident process."""

    operation_id: str
    sandbox_id: str
    sandbox_generation: int
    container_id: str
    runtime_id: str
    runtime_version: str | None
    source_thread_id: str
    source_native_session_id: str
    target_thread_id: str
    target_native_session_id: str
    target_resident_slot_id: int
    source_process_generation: int
    source_residency_generation: int
    target_process_generation: int
    target_residency_generation: int
    resident_state: Literal["live"] = "live"


@dataclass(frozen=True)
class SharedTurnTelemetry:
    """Kernel process events and exact hook marks closed at one turn boundary."""

    observation: GuestObservation
    tool_marks: tuple[ToolMark, ...] = ()
    #: The whole-turn member execution window, or None when this turn opened
    #: none -- a non-Kata runtime, or a guest whose helper has no window
    #: capability. None is "not measured here", never a zero.
    turn_resources: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        """Compatibility view for callers that previously received only it."""
        return self.observation.complete

    @property
    def source(self) -> str | None:
        """Observer identifier from the process result."""
        return self.observation.source

    @property
    def lost_events(self) -> int:
        """Lost event count from the process result."""
        return self.observation.lost_events

    @property
    def truncated(self) -> bool:
        """Whether the bounded process journal was truncated."""
        return self.observation.truncated

    @property
    def processes(self) -> tuple[Any, ...]:
        """Observed process rows before wire serialization."""
        return self.observation.processes


class SharedTurnBoundaryError(SandboxError):
    """A failed activation boundary that still carries its closed telemetry."""

    def __init__(self, detail: str, telemetry: SharedTurnTelemetry) -> None:
        super().__init__(detail)
        self.telemetry = telemetry


@dataclass
class _Member:
    thread_id: str
    native_session_id: str | None = None
    context_status: ContextStatus = "new"
    activation_generation: int = 0
    process_generation: int = 0
    residency_generation: int = 0
    resident_state: ResidentState = "stopped"
    resident_slot_id: int = 0
    last_settled_job_id: str | None = None
    lease_token: str | None = None
    lease_owner: str | None = None
    # Only the last completed activation can replay its acknowledgement.
    settlement_receipt: tuple[str, SharedSessionDescriptor] | None = field(default=None, repr=False)
    transport: ResidentTransport[DialectRuntime] | None = None
    current_turn: ResidentTurn[DialectRuntime] | None = None
    activation_processes: tuple[tuple[int, int], ...] | None = None
    observer_cursor: GuestObserverCursor | None = None
    tool_mark_reader: ToolMarkReader | None = None
    #: The member execution window this activation opened, if any. It is
    #: cleared with the cursor: a window belongs to exactly one activation,
    #: and a restarting member must never resume another one's.
    window: MemberWindowHandle | None = None
    window_sampler: MemberWindowSampler | None = None
    window_sequence: int = 0
    #: The last ``report_seq`` this window handed out. It is separate from the
    #: census counter because both keep moving: the sampler ticks while a
    #: closing census is in flight, so a final report that derived its sequence
    #: from the census counter could come out below a checkpoint that was
    #: produced in between -- and the store rejects a lower sequence, which
    #: would leave a normally completed turn stored as still collecting.
    window_report_seq: int = 0
    window_checkpoints: CheckpointSchedule | None = None
    #: When this window's next census is due, on the supervisor's clock.
    window_next_sample_mono: float = 0.0
    #: This window's cumulative measurement, kept across journal reads so a
    #: checkpoint describes the same counters the final report will.
    window_accumulator: WindowAccumulator | None = None
    #: The newest checkpoint the sampler produced and the turn has not
    #: forwarded yet.
    window_pending_checkpoint: dict[str, Any] | None = None
    #: How far the window's journal has been read. Only the offset is kept:
    #: the accumulator carries identity between slices, so retaining every byte
    #: read would grow without bound and re-parse it all on each tick.
    window_journal_offset: int = 0
    turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class _Group:
    spec: ChatSandboxSpec
    inspection: ChatSandboxInspection
    members: dict[str, _Member] = field(default_factory=dict)
    terminal_tokens: dict[str, str] = field(default_factory=dict)
    operation_tokens: dict[str, str] = field(default_factory=dict)
    snapshotting: bool = False
    environment_cancel: threading.Event | None = field(default=None, repr=False)
    environment_owner: str | None = field(default=None, repr=False)
    # A failed initial archive observed (or could not rule out) its durable
    # preparation marker, but the exact container could not yet be stopped.
    # Only a later accepted STOPPED inspection may release this snapshot fence.
    initial_archive_preservation_pending: bool = False
    starting: bool = False
    start_failed: bool = False
    stopping: bool = False
    archive_request_id: str | None = None
    stop_archive_pending: bool = False
    stop_archive_blocked: bool = False
    stop_archive_members: tuple[SharedMemberManifest, ...] | None = None
    orphan_finalization_pending: bool = False
    orphan_marker_unreadable: bool = False
    start_archive_completion_pending: bool = False
    container_lost: bool = False
    lifecycle_generation: int = 0
    lifecycle_check: _LifecycleCheck | None = field(default=None, repr=False)
    idle_deadline: float | None = None
    fork_mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True, eq=False)
class _LifecycleCheck:
    """Authority reserved while Docker or a durable marker is read without the lock."""

    group: _Group
    spec: ChatSandboxSpec
    container_id: str
    generation: int
    members: tuple[tuple[str, _Member], ...]


NativeForker = Callable[
    [ChatSandboxSpec, ResidentTransport[DialectRuntime], str, ResidentLaunch, int],
    str,
]
StoppedArchiveHook = Callable[
    [ChatSandboxSpec, ChatSandboxInspection, tuple[SharedMemberManifest, ...]],
    object,
]
StoppedArchivePrepareHook = Callable[
    [ChatSandboxSpec, ChatSandboxInspection],
    None,
]
ArchiveInspectionHook = Callable[[ChatSandboxSpec, ChatSandboxInspection], None]
MarkerlessFinalPrepareHook = Callable[[ChatSandboxSpec, ChatSandboxInspection], bool]

#: How long a message sent into a turn that has not started here is kept for
#: it. Long past any sandbox preparation; a turn that never starts leaves its
#: message to the control plane, which sends it as the next turn instead.
HELD_INPUT_TTL_S = 15 * 60.0
#: The pre-prompt archive's bound on waiting for a concurrent bridge check or
#: workspace request (the chat page reads the workspace while a chat prepares).
INITIAL_ARCHIVE_WAIT_S = 30.0


@dataclass(frozen=True)
class _HeldInput:
    """A message for an owner's turn that has not started on this sandbox manager yet."""

    owner: str
    pending: PendingInput
    held_at: float


class SharedChatSessionSupervisor:
    """Own one live process per member and one Docker lifecycle per group."""

    def __init__(
        self,
        manager: ChatSandboxManager,
        *,
        idle_timeout_s: float = DEFAULT_CHAT_IDLE_TIMEOUT_S,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        transport_factory: ResidentTransportFactory | None = None,
        native_forker: NativeForker | None = None,
        checkpoint_interval_s: float = RUNNING_CHECKPOINT_INTERVAL_S,
        window_sample_interval_s: float = MEMBER_WINDOW_SAMPLE_INTERVAL_MS / 1000,
        window_checkpoint_hook: Callable[[str, dict[str, Any]], None] | None = None,
        stopped_archive_prepare_hook: StoppedArchivePrepareHook | None = None,
        stopped_archive_invalidate_hook: ArchiveInspectionHook | None = None,
        starting_archive_prepare_hook: ArchiveInspectionHook | None = None,
        started_archive_hook: ArchiveInspectionHook | None = None,
        markerless_final_prepare_hook: MarkerlessFinalPrepareHook | None = None,
        stopped_archive_hook: StoppedArchiveHook | None = None,
    ) -> None:
        if idle_timeout_s <= 0 or sweep_interval_s <= 0:
            raise ValueError("shared sandbox timeout values must be positive")
        self._checkpoint_interval_s = max(0.0, checkpoint_interval_s)
        #: Called with (measurement_id, cumulative snapshot) while a turn is
        #: still running, so a crash keeps the work measured up to the last
        #: checkpoint. It must never raise into the turn.
        self._window_checkpoint_hook = window_checkpoint_hook
        #: The census period. One second is what the summary reports and what
        #: a reader compares across turns, so the sampler owns it.
        self._sample_interval_s = window_sample_interval_s if window_sample_interval_s > 0 else 1.0
        self._sample_tick_s = min(0.25, self._sample_interval_s / 4)
        self._sampler_thread: threading.Thread | None = None
        self._manager = manager
        self._idle_timeout_s = idle_timeout_s
        self._sweep_interval_s = sweep_interval_s
        self._idle_host_rules_failing = False
        self._clock = clock
        self._transport_factory = transport_factory or ResidentTransportFactory(manager)
        self._native_forker = native_forker or self._transport_factory.fork_native
        # Called only after Docker has cleanly stopped the exact generation,
        # while a lifecycle barrier still blocks concurrent workspace writes.
        # It is synchronous so success means the durable final object exists;
        # failure remains pending and prevents the group from restarting.
        self._stopped_archive_prepare_hook = stopped_archive_prepare_hook
        self._stopped_archive_invalidate_hook = stopped_archive_invalidate_hook
        self._starting_archive_prepare_hook = starting_archive_prepare_hook
        self._started_archive_hook = started_archive_hook
        self._markerless_final_prepare_hook = markerless_final_prepare_hook
        self._stopped_archive_hook = stopped_archive_hook
        self._groups: dict[str, _Group] = {}
        self._members: dict[str, _Group] = {}
        #: Sandboxes a registration is creating or adopting outside the lock.
        #: The group does not exist yet, so this set is their admission barrier.
        self._creating: set[str] = set()
        self._held_inputs: dict[str, list[_HeldInput]] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: How long the pre-prompt archive waits for a concurrent bridge check
        #: or workspace request to finish before it reports the group busy.
        self.initial_archive_wait_s = INITIAL_ARCHIVE_WAIT_S

    @property
    def idle_timeout_s(self) -> float:
        """Return the one group inactivity timeout."""
        return self._idle_timeout_s

    def set_idle_timeout_s(self, idle_timeout_s: float) -> None:
        """Change the inactivity timeout at runtime, as the admin page asks.

        A group's deadline is armed when it goes quiet, so the new value
        applies from each group's next quiet moment; a deadline already armed
        keeps the value it was armed with rather than moving under a group that
        is about to be stopped.
        """
        if idle_timeout_s <= 0:
            raise ValueError("shared sandbox timeout values must be positive")
        with self._lock:
            self._idle_timeout_s = idle_timeout_s

    def configure_stopped_archive_hook(self, hook: StoppedArchiveHook) -> None:
        """Bind the durable final-archive hook before lifecycle admission.

        ``create_app`` also accepts an injected supervisor, so constructor
        wiring alone cannot prove that clean stops reach the archive service.
        Permit one pre-start binding, accept an idempotent verification of the
        same callback, and reject every live or conflicting reconfiguration.
        """
        if not callable(hook):
            raise TypeError("shared sandbox stopped archive hook must be callable")
        with self._lock:
            current = self._stopped_archive_hook
            if current is not None:
                if current != hook:
                    raise ValueError("shared sandbox stopped archive hook is already configured")
                return
            if self._thread is not None or self._groups:
                raise ValueError(
                    "shared sandbox stopped archive hook must be configured before start"
                )
            self._stopped_archive_hook = hook

    def configure_stopped_archive_prepare_hook(
        self,
        hook: StoppedArchivePrepareHook,
    ) -> None:
        """Bind the durable pre-stop policy fence before lifecycle admission."""
        if not callable(hook):
            raise TypeError("shared sandbox stopped archive prepare hook must be callable")
        with self._lock:
            current = self._stopped_archive_prepare_hook
            if current is not None:
                if current != hook:
                    raise ValueError(
                        "shared sandbox stopped archive prepare hook is already configured"
                    )
                return
            if self._thread is not None or self._groups:
                raise ValueError(
                    "shared sandbox stopped archive prepare hook must be configured before start"
                )
            self._stopped_archive_prepare_hook = hook

    def configure_archive_transition_hooks(
        self,
        *,
        invalidate_final: ArchiveInspectionHook,
        prepare_start: ArchiveInspectionHook,
        complete_start: ArchiveInspectionHook,
        prepare_markerless_final: MarkerlessFinalPrepareHook,
    ) -> None:
        """Bind marker transitions that bracket start and failed native flush."""
        hooks = (
            invalidate_final,
            prepare_start,
            complete_start,
            prepare_markerless_final,
        )
        if not all(callable(hook) for hook in hooks):
            raise TypeError("shared sandbox archive transition hooks must be callable")
        with self._lock:
            current = (
                self._stopped_archive_invalidate_hook,
                self._starting_archive_prepare_hook,
                self._started_archive_hook,
                self._markerless_final_prepare_hook,
            )
            if any(hook is not None for hook in current):
                if current != hooks:
                    raise ValueError(
                        "shared sandbox archive transition hooks are already configured"
                    )
                return
            if self._thread is not None or self._groups:
                raise ValueError(
                    "shared sandbox archive transition hooks must be configured before start"
                )
            (
                self._stopped_archive_invalidate_hook,
                self._starting_archive_prepare_hook,
                self._started_archive_hook,
                self._markerless_final_prepare_hook,
            ) = hooks

    def _prepare_markerless_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
    ) -> bool:
        """Ask the dynamic policy owner to authorize markerless cleanup.

        A static archive service has no such authority and therefore keeps the
        conservative quarantine behavior.  The dynamic controller writes the
        disabled-policy marker atomically before returning ``True``.
        """
        hook = self._markerless_final_prepare_hook
        if self._stopped_archive_hook is None or hook is None:
            return False
        try:
            return hook(spec, inspection)
        except Exception:
            logger.exception(
                "markerless sandbox policy fence could not be prepared",
                extra={
                    "event": "sandbox_markerless_final_prepare_failed",
                    "sandbox_id": spec.sandbox_id,
                    "generation": spec.generation,
                },
            )
            return False

    def start(self) -> None:
        """Reconstruct shared containers and start the group idle sweep."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
        # Reconciliation may synchronously upload a final archive.  Do not
        # retain the supervisor-wide metadata lock while that bounded I/O is
        # in flight: unrelated status calls must remain responsive.
        self.reconcile()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._sweep_loop,
                daemon=True,
                name="shared-chat-sandbox-idle-sweep",
            )
            self._thread.start()
            if self._sampler_thread is None or not self._sampler_thread.is_alive():
                self._sampler_thread = threading.Thread(
                    target=self._sample_loop,
                    daemon=True,
                    name="shared-member-window-sampler",
                )
                self._sampler_thread.start()

    def quiesce(self) -> None:
        """Stop background admission without detaching live native transports."""
        self._stop.set()
        threads = (self._thread, self._sampler_thread)
        current_thread = threading.current_thread()
        if any(thread is current_thread for thread in threads):
            raise RuntimeError("cannot close the supervisor from its background thread")
        # A sweep may already be inside the synchronous final-archive hook.
        # Its upload is bounded by the archive service's operation deadline,
        # so wait for completion instead of abandoning a daemon thread and
        # closing the service underneath it.  The sampler is joined for the
        # same ownership guarantee.
        for thread in threads:
            if thread is not None:
                thread.join()
        with self._lock:
            self._thread = None
            self._sampler_thread = None

    def close(self) -> None:
        """Stop the supervisor process; leave Docker-owned state intact."""
        self.quiesce()
        # Pipes cannot be reattached by a replacement supervisor. Close them
        # explicitly; native locators remain in Docker and are resumed later.
        with self._lock:
            transports = [
                member.transport
                for group in self._groups.values()
                for member in group.members.values()
                if member.transport is not None
            ]
            observations = [
                (group.spec, member.observer_cursor)
                for group in self._groups.values()
                for member in group.members.values()
                if member.observer_cursor is not None
            ]
            turn_locks = [
                member.turn_lock
                for group in self._groups.values()
                for member in group.members.values()
                # An initialization may also hold this lock, but its context
                # manager is still alive and owns the release. Only a detached
                # admitted turn loses the normal ``complete_turn`` release
                # path when ``close`` clears ``current_turn`` below.
                if member.current_turn is not None and member.turn_lock.locked()
            ]
            for group in self._groups.values():
                for member in group.members.values():
                    member.transport = None
                    member.current_turn = None
                    member.activation_processes = None
                    member.observer_cursor = None
                    member.tool_mark_reader = None
                    member.window = None
                    member.window_sampler = None
                    member.window_checkpoints = None
                    member.window_accumulator = None
                    if member.resident_state == "live":
                        member.resident_state = "restarting"
        for transport in transports:
            with contextlib.suppress(Exception):
                transport.close()
        # Closing a transport ends the member workload. Drain afterwards so
        # the manager also releases its per-container active-cursor fence;
        # otherwise restarting this supervisor object can suppress safe
        # journal rotation for the rest of the container lifetime.
        for spec, cursor in observations:
            with contextlib.suppress(Exception):
                self._manager.finish_guest_observation(spec, cursor)
        # ``close`` may be used by a supervisor restart in the same process,
        # not only interpreter shutdown. Do not leave a detached active turn's
        # member lock permanently acquired after its transport and observation
        # have both been closed.
        for turn_lock in turn_locks:
            with contextlib.suppress(RuntimeError):
                turn_lock.release()

    def stop_all_for_shutdown(self, *, include_running: bool = True) -> None:
        """Cleanly stop and archive every running group during sandbox manager shutdown.

        The sandbox manager calls this only when final archival is configured, after
        :meth:`quiesce` has stopped background admission but before
        :meth:`close` detaches native transports. A failed archive remains
        pending and is reported after the other groups have also had a chance
        to stop.
        """
        with self._lock:
            candidates = [
                (next(iter(group.members)), group.spec.generation, group)
                for group in self._groups.values()
                if group.members
                and (
                    (include_running and group.inspection.running)
                    or group.stop_archive_pending
                    or group.orphan_finalization_pending
                    or group.start_archive_completion_pending
                )
            ]
        failures: builtins.list[Exception] = []
        for thread_id, generation, group in candidates:
            with self._lock:
                retry_pending = group.stop_archive_pending
                blocked = group.stop_archive_blocked
                if retry_pending:
                    if group.stopping:
                        failures.append(
                            SessionBusy("shared sandbox group stop is already in progress")
                        )
                        continue
                    group.stopping = True
            if retry_pending:
                try:
                    if blocked:
                        self._stop_blocked_group(group)
                        with self._lock:
                            still_blocked = group.stop_archive_blocked
                        if still_blocked:
                            failures.append(
                                SandboxError(
                                    "shared sandbox final archive skip could not be persisted"
                                )
                            )
                    else:
                        self._retry_stopped_archive(group)
                except Exception as exc:
                    failures.append(exc)
                finally:
                    with self._lock:
                        group.stopping = False
                        self._condition.notify_all()
                continue
            try:
                self.stop_if_idle(
                    thread_id,
                    generation=generation,
                    reason="orphaned_attempt",
                )
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise SandboxError(
                f"{len(failures)} shared sandbox shutdown stop/archive operation(s) failed"
            ) from failures[0]

    def shutdown_archive_candidate_count(self) -> int:
        """Return how many groups shutdown must stop or finish archiving."""
        with self._lock:
            return sum(
                group.inspection.running
                or group.stop_archive_pending
                or group.orphan_finalization_pending
                or group.start_archive_completion_pending
                for group in self._groups.values()
            )

    def has_pending_archive_work(self) -> bool:
        """Whether shutdown must preserve or finish an older archive lifecycle."""
        with self._lock:
            return any(
                group.stop_archive_pending
                or group.stop_archive_blocked
                or group.orphan_finalization_pending
                or group.start_archive_completion_pending
                for group in self._groups.values()
            )

    @staticmethod
    def _archive_members_locked(group: _Group) -> tuple[SharedMemberManifest, ...]:
        """Snapshot final-archive membership while the supervisor lock is held."""
        return tuple(
            SharedMemberManifest(
                thread_id=member.thread_id,
                native_session_id=member.native_session_id,
                context_status=member.context_status,
                activation_generation=member.activation_generation,
                process_generation=member.process_generation,
                residency_generation=member.residency_generation,
                resident_state=member.resident_state,
                resident_slot_id=member.resident_slot_id,
                last_settled_job_id=member.last_settled_job_id,
            )
            for member in sorted(group.members.values(), key=lambda item: item.thread_id)
        )

    @staticmethod
    def _mark_container_lost_locked(group: _Group) -> None:
        """Make a same-name replacement permanently inadmissible to this group."""
        if not group.container_lost:
            group.lifecycle_generation += 1
        group.container_lost = True
        group.idle_deadline = None
        for member in group.members.values():
            member.context_status = "lost"
            member.resident_state = "lost"

    def _accept_inspection_locked(
        self,
        group: _Group,
        inspection: ChatSandboxInspection,
    ) -> ChatSandboxInspection:
        """Refresh status only for the immutable Docker object first adopted.

        Docker names and ownership labels are reusable.  The container ID in
        the group's first inspection is therefore its lifetime fence: never
        replace it with a later same-name object, especially not before a
        final archive whose key belongs to the original generation.
        """
        if inspection.container_id != group.inspection.container_id:
            self._mark_container_lost_locked(group)
            raise RetainedChatLost("shared sandbox container identity changed")
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        group.inspection = inspection
        if group.initial_archive_preservation_pending and not inspection.running:
            group.initial_archive_preservation_pending = False
            group.snapshotting = False
            self._condition.notify_all()
        return inspection

    def _begin_lifecycle_check_locked(self, group: _Group) -> _LifecycleCheck:
        """Reserve this group, leaving the metadata lock free during external I/O."""
        if group.lifecycle_check is not None:
            raise SessionBusy("shared sandbox lifecycle check is in progress")
        group.lifecycle_generation += 1
        check = _LifecycleCheck(
            group,
            group.spec,
            group.inspection.container_id,
            group.lifecycle_generation,
            tuple(group.members.items()),
        )
        group.lifecycle_check = check
        return check

    def _require_lifecycle_check_locked(self, check: _LifecycleCheck) -> None:
        """Reject results whose group, members or lifecycle authority was superseded."""
        group = check.group
        if (
            self._groups.get(check.spec.sandbox_id) is not group
            or group.spec != check.spec
            or group.inspection.container_id != check.container_id
            or group.lifecycle_generation != check.generation
            or group.lifecycle_check is not check
            or len(group.members) != len(check.members)
            or any(
                group.members.get(thread_id) is not member
                or self._members.get(thread_id) is not group
                for thread_id, member in check.members
            )
        ):
            raise SessionLeaseLost("shared sandbox lifecycle changed during observation")

    def _end_lifecycle_check_locked(self, check: _LifecycleCheck) -> None:
        """Release only this operation's reservation, including on failure."""
        if check.group.lifecycle_check is check:
            check.group.lifecycle_check = None
            check.group.lifecycle_generation += 1
            self._condition.notify_all()

    def _read_final_marker(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> bytes | None:
        """Read the exact object's marker with no supervisor metadata lock held."""
        if inspection.running:
            return self._manager.read_running_final_archive_marker(
                spec,
                expected_container_id=inspection.container_id,
            )
        return self._manager.read_stopped_final_archive_marker_optional(
            spec,
            expected_container_id=inspection.container_id,
        )

    @staticmethod
    def _marker_result(payload: bytes | None, error: BaseException | None) -> bytes | None:
        """Apply an out-of-lock marker read inside its original failure handling."""
        if error is not None:
            raise error
        return payload

    def _require_current_inspection(self, group: _Group) -> ChatSandboxInspection:
        """Inspect and accept only the exact Docker object owned by ``group``."""
        current = self._manager.inspect(group.spec.container_name)
        with self._lock:
            if current is None:
                self._mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            return self._accept_inspection_locked(group, current)

    def _persist_stopped_archive(
        self,
        group: _Group,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> ChatSandboxInspection:
        """Persist one clean stop, then release its durable retry marker."""
        if inspection.running:
            raise SandboxError("refusing to archive a running shared sandbox as stopped")
        with self._lock:
            self._accept_inspection_locked(group, inspection)
            if self._stopped_archive_hook is None:
                group.stop_archive_pending = False
                group.stop_archive_blocked = False
                group.stop_archive_members = None
                group.orphan_finalization_pending = False
                group.orphan_marker_unreadable = False
                group.start_archive_completion_pending = False
                return inspection
            group.stop_archive_pending = True
            group.stop_archive_members = members
        try:
            self._stopped_archive_hook(group.spec, inspection, members)
        except Exception as exc:
            logger.error(
                "stopped sandbox final archive failed",
                extra={
                    "event": "sandbox_final_archive_failed",
                    "sandbox_id": group.spec.sandbox_id,
                    "generation": group.spec.generation,
                    "detail": _archive_failure_diagnostic("fail_stop_final", exc).render(),
                },
            )
            raise
        # The owned bridge is the durable marker that a stopped container has
        # not completed its final archive. Release it only after the upload
        # returned successfully, using the same exact-container fence as the
        # original stop. A cleanup failure leaves ``stop_archive_pending`` and
        # the bridge intact so this operation is retried rather than reported
        # as a clean stop.
        released = self._manager.stop(
            group.spec,
            expected_container_id=inspection.container_id,
            release_networks=True,
        )
        with self._lock:
            self._accept_inspection_locked(group, released)
            group.stop_archive_pending = False
            group.stop_archive_blocked = False
            group.stop_archive_members = None
            group.orphan_finalization_pending = False
            group.orphan_marker_unreadable = False
            group.start_archive_completion_pending = False
        return released

    def _retry_stopped_archive(self, group: _Group, *, persist: bool = True) -> None:
        """Finish a prepared stop, optionally deferring archive persistence."""
        with self._lock:
            members = group.stop_archive_members
            pending = group.stop_archive_pending
        if not pending or members is None:
            return
        inspection = self._require_current_inspection(group)
        if inspection.running:
            if self._stopped_archive_prepare_hook is not None:
                try:
                    self._stopped_archive_prepare_hook(group.spec, inspection)
                except Exception:
                    if not persist:
                        # Network fail-stop phase one cannot leave a group
                        # running merely because its prepared marker is no
                        # longer usable. Skip this lifecycle's final archive:
                        # this group must never upload unproven history.
                        with self._lock:
                            group.stop_archive_blocked = True
                            group.stop_archive_pending = True
                            group.stop_archive_members = members
                        self._stop_blocked_group(group, inspection)
                        with self._lock:
                            if not group.stop_archive_blocked:
                                return
                    raise
            inspection = self._manager.stop(
                group.spec,
                expected_container_id=inspection.container_id,
                release_networks=self._stopped_archive_hook is None,
            )
            with self._lock:
                self._accept_inspection_locked(group, inspection)
        with self._lock:
            for member in group.members.values():
                member.resident_state = (
                    "lost"
                    if member.resident_state == "lost" or member.context_status == "lost"
                    else "stopped"
                )
            members = self._archive_members_locked(group)
            group.stop_archive_members = members
        if not persist:
            return
        self._persist_stopped_archive(group, inspection, members)

    def _stop_blocked_group(
        self,
        group: _Group,
        inspection: ChatSandboxInspection | None = None,
    ) -> ChatSandboxInspection:
        """Exact-stop a group whose native history is unproven, then finish without upload.

        The invalidate hook durably records a skipped final. Once it has, the
        group finishes its lifecycle like any clean stop: the bridge is released
        and the group can start and resume again. Without that durable record
        (the hook failed, or a static archive service cannot write one) the
        group stays fenced with its bridge retained, and reconciliation retries.
        """
        current = inspection or self._require_current_inspection(group)
        marker_persisted = False
        if self._stopped_archive_invalidate_hook is not None:
            try:
                self._stopped_archive_invalidate_hook(group.spec, current)
                marker_persisted = True
            except Exception:
                logger.exception(
                    "blocked sandbox marker retry failed",
                    extra={
                        "event": "sandbox_archive_block_marker_retry_failed",
                        "sandbox_id": group.spec.sandbox_id,
                        "generation": group.spec.generation,
                    },
                )
        if current.running:
            # Without archive hooks there is no retry marker to keep.
            current = self._manager.stop(
                group.spec,
                expected_container_id=current.container_id,
                release_networks=self._stopped_archive_hook is None,
            )
            with self._lock:
                self._accept_inspection_locked(group, current)
        if not marker_persisted and self._stopped_archive_invalidate_hook is not None:
            try:
                self._stopped_archive_invalidate_hook(group.spec, current)
                marker_persisted = True
            except Exception:
                logger.exception(
                    "stopped sandbox block marker retry failed",
                    extra={
                        "event": "sandbox_archive_stopped_block_marker_retry_failed",
                        "sandbox_id": group.spec.sandbox_id,
                        "generation": group.spec.generation,
                    },
                )
        if not marker_persisted and self._stopped_archive_hook is not None:
            return current
        with self._lock:
            members = group.stop_archive_members or self._archive_members_locked(group)
        # The skipped marker makes the final hook a no-op, so this only
        # releases the bridge and clears the fences.
        return self._persist_stopped_archive(group, current, members)

    def _wait_for_start_locked(self, group: _Group, *, retry_failed: bool = False) -> None:
        """Wait for an in-flight bridge check without holding the metadata lock.

        Workspace reads and terminal opens revalidate even a running container.
        Their temporary start fence must not reject an already leased member's
        prompt. Callers recheck membership and activation after this wait.
        """
        deadline = time.monotonic() + 30.0
        while group.starting:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionBusy("shared workspace start is in progress")
            self._condition.wait(timeout=min(remaining, 0.5))
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        if group.start_failed and not retry_failed:
            raise SandboxError("shared workspace start verification failed")

    def _start_group(
        self,
        group: _Group,
        *,
        facts: SandboxActionFacts | None = None,
        observer: StageRecorder | None = None,
    ) -> ChatSandboxInspection:
        """Start a group and archive a guest-network fail-stop exactly once."""
        with self._lock:
            self._wait_for_start_locked(group, retry_failed=True)
            if group.lifecycle_check is not None:
                raise SessionBusy("shared sandbox lifecycle check is in progress")
            if group.stopping:
                raise SessionBusy("shared workspace stop is in progress")
            if group.stop_archive_pending:
                raise SessionBusy("shared workspace final archive retry is pending")
            if group.stop_archive_blocked:
                raise SessionBusy(
                    "shared workspace final archive is blocked by native flush failure"
                )
            if group.orphan_finalization_pending:
                raise SessionBusy("shared workspace orphan finalization is pending")
            group.starting = True
            members = self._archive_members_locked(group)
            expected_container_id = group.inspection.container_id
        start_fence_prepared = False
        start_completed = False

        def prepare_stopped_start(current: ChatSandboxInspection) -> None:
            nonlocal start_fence_prepared
            if self._starting_archive_prepare_hook is None:
                return
            self._starting_archive_prepare_hook(group.spec, current)
            with self._lock:
                self._accept_inspection_locked(group, current)
                group.start_archive_completion_pending = True
                start_fence_prepared = True

        try:
            with self._lock:
                completion_pending = group.start_archive_completion_pending
            if completion_pending:
                current = self._require_current_inspection(group)
                if current.running:
                    if self._started_archive_hook is None:
                        raise SandboxError(
                            "shared workspace start archive completion hook is unavailable"
                        )
                    # ``start_archive_completion_pending`` is set only after
                    # ``manager.start`` returned a fully network-verified
                    # running container and the durable marker completion hook
                    # failed. Retry that idempotent completion in place. A cold
                    # stop here would introduce a markerless crash window after
                    # the completion hook had already cleared the marker.
                    self._started_archive_hook(group.spec, current)
                    with self._lock:
                        self._accept_inspection_locked(group, current)
                        group.start_archive_completion_pending = False
                    start_completed = True
                    return current
            if self._starting_archive_prepare_hook is None:
                inspection = self._manager.start(
                    group.spec,
                    facts=facts,
                    observer=observer,
                    expected_container_id=expected_container_id,
                    defer_network_release_on_fail_stop=(self._stopped_archive_hook is not None),
                )
            else:
                inspection = self._manager.start(
                    group.spec,
                    facts=facts,
                    observer=observer,
                    expected_container_id=expected_container_id,
                    defer_network_release_on_fail_stop=(self._stopped_archive_hook is not None),
                    before_stopped_start=prepare_stopped_start,
                )
        except RetainedChatLost:
            with self._lock:
                self._mark_container_lost_locked(group)
            raise
        except RetainedChatFailStopped as exc:
            with self._lock:
                self._accept_inspection_locked(group, exc.inspection)
            self._persist_stopped_archive(group, exc.inspection, members)
            raise
        except BaseException:
            if not start_fence_prepared:
                raise
            # Once the durable start fence exists, any untyped manager error
            # is ambiguous: Docker may have started the container before guest
            # network synchronization or proof failed. Never admit reuse from
            # that state. Exact-stop it with the bridge retained and complete
            # the same fail-stop archive path; if stopping itself fails, the
            # pending barrier remains for reconcile/shutdown to retry.
            with self._lock:
                group.stop_archive_pending = self._stopped_archive_hook is not None
                group.stop_archive_members = (
                    members if self._stopped_archive_hook is not None else None
                )
            uncertain = self._require_current_inspection(group)
            if uncertain.running:
                uncertain = self._manager.stop(
                    group.spec,
                    expected_container_id=expected_container_id,
                    release_networks=self._stopped_archive_hook is None,
                )
            self._persist_stopped_archive(group, uncertain, members)
            raise
        else:
            with self._lock:
                completion_pending = group.start_archive_completion_pending
            if (
                start_fence_prepared or completion_pending
            ) and self._started_archive_hook is not None:
                self._started_archive_hook(group.spec, inspection)
            with self._lock:
                self._accept_inspection_locked(group, inspection)
                if start_fence_prepared or completion_pending:
                    group.start_archive_completion_pending = False
            start_completed = True
            return inspection
        finally:
            with self._lock:
                group.start_failed = not start_completed
                group.starting = False
                self._condition.notify_all()

    def reconcile(self) -> builtins.list[SharedSessionDescriptor]:
        """Discover containers, fencing each group's slow recovery independently."""
        with self._lock:
            before = {
                key: (group, group.lifecycle_generation) for key, group in self._groups.items()
            }
        found: builtins.list[SharedSessionDescriptor] = []
        specs: builtins.list[ChatSandboxSpec] = []
        for inspection in self._manager.list():
            if inspection.labels.get(CHAT_SHARED_LABEL) != "true":
                continue
            spec = self._spec_from_inspection(inspection)
            specs.append(spec)
            with self._lock:
                group = self._groups.get(spec.sandbox_id)
                previous = before.get(spec.sandbox_id)
                if group is not None and (
                    (
                        previous is None
                        or previous[0] is not group
                        or previous[1] != group.lifecycle_generation
                    )
                    or group.lifecycle_check is not None
                    or group.starting
                    or group.stopping
                ):
                    found.extend(self._descriptors(group))
                    continue
                if group is None and spec.sandbox_id in self._creating:
                    continue
                discovered = group is None
                if group is None:
                    group = _Group(spec=spec, inspection=inspection)
                    origin = _Member(thread_id=spec.thread_id, resident_slot_id=0)
                    group.members[origin.thread_id] = origin
                    self._groups[spec.sandbox_id] = group
                    self._members[origin.thread_id] = group
                else:
                    if group.spec != spec:
                        raise SandboxError("shared sandbox immutable labels changed")
                    self._accept_inspection_locked(group, inspection)
                read_marker = (
                    discovered
                    or group.orphan_marker_unreadable
                    or (
                        not inspection.running
                        and inspection.status is not ChatSandboxStatus.CREATED
                        and self._manager.network_for(spec) in inspection.networks
                        and self._is_quiescent(group)
                        and not group.stop_archive_blocked
                        and not group.orphan_finalization_pending
                        and not group.stop_archive_pending
                    )
                )
                if not (
                    read_marker
                    or group.stop_archive_blocked
                    or group.orphan_finalization_pending
                    or group.stop_archive_pending
                ):
                    found.extend(self._descriptors(group))
                    continue
                check = self._begin_lifecycle_check_locked(group)
            try:
                descriptors = self._reconcile_checked_group(
                    check, inspection, discovered=discovered, read_marker=read_marker
                )
            finally:
                with self._lock:
                    self._end_lifecycle_check_locked(check)
            with self._lock:
                found.extend(
                    replace(item, group_busy=not self._is_quiescent(group)) for item in descriptors
                )
        self._manager.prune_orphan_networks(specs)
        return found

    def _reconcile_checked_group(
        self,
        check: _LifecycleCheck,
        inspection: ChatSandboxInspection,
        *,
        discovered: bool,
        read_marker: bool,
    ) -> builtins.list[SharedSessionDescriptor]:
        """Classify and recover the reserved exact group without holding the global lock."""
        group, spec = check.group, check.spec
        found: builtins.list[SharedSessionDescriptor] = []
        marker_payload = None
        marker_error = None
        marker_prepared = False
        if read_marker:
            try:
                marker_payload = self._read_final_marker(spec, inspection)
                if (
                    marker_payload is None
                    and inspection.status is not ChatSandboxStatus.CREATED
                    and (
                        inspection.running or self._manager.network_for(spec) in inspection.networks
                    )
                ):
                    marker_prepared = self._prepare_markerless_final(spec, inspection)
            except BaseException as exc:
                marker_error = exc
        owned_bridge = self._manager.network_for(spec)
        finalize = False
        retry = False
        blocked = False
        invalidate_unknown = False
        marker_unreadable = False
        members: tuple[SharedMemberManifest, ...] = ()
        with self._lock:
            self._require_lifecycle_check_locked(check)
            if discovered:
                try:
                    marker_payload = self._marker_result(marker_payload, marker_error)
                except BaseException:
                    # A marker read is an authority check, not optional
                    # observability. Keep the adopted group fenced if its
                    # sandbox-manager-private lifecycle record is unreadable, but do
                    # not let one damaged orphan prevent healthy groups from
                    # reconciling and receiving the network guard.
                    group.orphan_finalization_pending = True
                    group.orphan_marker_unreadable = True
                    marker_unreadable = True
                    logger.exception(
                        "sandbox final archive marker could not be classified",
                        extra={
                            "event": "sandbox_archive_marker_unreadable",
                            "sandbox_id": spec.sandbox_id,
                            "generation": spec.generation,
                        },
                    )
                else:
                    marker_present = marker_payload is not None
                    if (
                        not marker_present
                        and inspection.status is not ChatSandboxStatus.CREATED
                        and (inspection.running or owned_bridge in inspection.networks)
                    ):
                        marker_present = marker_prepared
                    unknown_unflushed = (
                        self._stopped_archive_hook is not None
                        and not marker_present
                        and inspection.status is not ChatSandboxStatus.CREATED
                        and (inspection.running or owned_bridge in inspection.networks)
                    )
                    if unknown_unflushed:
                        members = self._archive_members_locked(group)
                        group.stop_archive_pending = True
                        group.stop_archive_blocked = True
                        group.stop_archive_members = members
                        group.stopping = inspection.running
                        blocked = True
                        if inspection.running and self._stopped_archive_invalidate_hook is not None:
                            invalidate_unknown = True
                    else:
                        # With a marker, its original policy and phase are
                        # authoritative even if an external actor removed the
                        # bridge. Without archive hooks, retain legacy cleanup.
                        finalize = marker_present or (
                            self._stopped_archive_hook is None
                            and (inspection.running or owned_bridge in inspection.networks)
                        )
                    if finalize:
                        group.orphan_finalization_pending = True
                        group.stopping = True
                        members = self._archive_members_locked(group)
            else:
                if group.spec != spec:
                    raise SandboxError("shared sandbox immutable labels changed")
                self._accept_inspection_locked(group, inspection)
                if group.orphan_marker_unreadable:
                    try:
                        marker_payload = self._marker_result(marker_payload, marker_error)
                    except BaseException:
                        marker_unreadable = True
                        logger.exception(
                            "sandbox final archive marker remains unreadable",
                            extra={
                                "event": "sandbox_archive_marker_still_unreadable",
                                "sandbox_id": spec.sandbox_id,
                                "generation": spec.generation,
                            },
                        )
                    else:
                        group.orphan_marker_unreadable = False
                        marker_present = marker_payload is not None
                        if (
                            not marker_present
                            and inspection.status is not ChatSandboxStatus.CREATED
                            and (inspection.running or owned_bridge in inspection.networks)
                        ):
                            marker_present = marker_prepared
                        unknown_unflushed = (
                            self._stopped_archive_hook is not None
                            and not marker_present
                            and inspection.status is not ChatSandboxStatus.CREATED
                            and (inspection.running or owned_bridge in inspection.networks)
                        )
                        if unknown_unflushed:
                            members = self._archive_members_locked(group)
                            group.stop_archive_pending = True
                            group.stop_archive_blocked = True
                            group.stop_archive_members = members
                            group.orphan_finalization_pending = False
                            group.stopping = inspection.running
                            blocked = True
                            if (
                                inspection.running
                                and self._stopped_archive_invalidate_hook is not None
                            ):
                                invalidate_unknown = True
                        elif marker_present:
                            group.stopping = True
                            finalize = True
                            members = self._archive_members_locked(group)
                        else:
                            group.orphan_finalization_pending = False
                elif group.stop_archive_blocked:
                    group.stopping = inspection.running
                    blocked = True
                elif group.orphan_finalization_pending:
                    if group.stopping:
                        raise SessionBusy(
                            "shared sandbox orphan finalization is already in progress"
                        )
                    group.stopping = True
                    finalize = True
                    members = self._archive_members_locked(group)
                elif group.stop_archive_pending:
                    if group.stopping:
                        raise SessionBusy("shared sandbox group stop is already in progress")
                    group.stopping = True
                    retry = True
                elif (
                    not inspection.running
                    and inspection.status is not ChatSandboxStatus.CREATED
                    and owned_bridge in inspection.networks
                    and self._is_quiescent(group, allow_lifecycle_check=True)
                ):
                    # Bridge presence alone is not authority to invent a
                    # clean final archive. Classify the durable marker first:
                    # present means retry its exact lifecycle; absent means
                    # native history is unknown and must remain blocked.
                    try:
                        marker_payload = self._marker_result(marker_payload, marker_error)
                    except BaseException:
                        group.orphan_finalization_pending = True
                        group.orphan_marker_unreadable = True
                        marker_unreadable = True
                        logger.exception(
                            "stopped sandbox final archive marker is unreadable",
                            extra={
                                "event": "sandbox_stopped_archive_marker_unreadable",
                                "sandbox_id": spec.sandbox_id,
                                "generation": spec.generation,
                            },
                        )
                    else:
                        members = self._archive_members_locked(group)
                        marker_present = marker_payload is not None
                        if not marker_present:
                            marker_present = marker_prepared
                        if not marker_present and self._stopped_archive_hook is not None:
                            group.stop_archive_pending = True
                            group.stop_archive_blocked = True
                            group.stop_archive_members = members
                            blocked = True
                        else:
                            if group.stopping:
                                raise SessionBusy(
                                    "shared sandbox group stop is already in progress"
                                )
                            group.stopping = True
                            finalize = True

        if marker_unreadable:
            if inspection.running:
                try:
                    inspection = self._manager.stop(
                        spec,
                        expected_container_id=inspection.container_id,
                        release_networks=False,
                    )
                except Exception:
                    logger.exception(
                        "unreadable-marker sandbox could not be exact-stopped",
                        extra={
                            "event": "sandbox_archive_marker_quarantine_stop_failed",
                            "sandbox_id": spec.sandbox_id,
                            "generation": spec.generation,
                        },
                    )
                else:
                    with self._lock:
                        self._accept_inspection_locked(group, inspection)
            with self._lock:
                found.extend(self._descriptors(group))
            return found

        if invalidate_unknown:
            try:
                assert self._stopped_archive_invalidate_hook is not None
                self._stopped_archive_invalidate_hook(spec, inspection)
            except Exception:
                logger.exception(
                    "unknown running orphan block marker could not be persisted",
                    extra={
                        "event": "sandbox_orphan_block_marker_failed",
                        "sandbox_id": spec.sandbox_id,
                        "generation": spec.generation,
                    },
                )

        if blocked:
            try:
                inspection = self._stop_blocked_group(group, inspection)
            except Exception:
                logger.exception(
                    "blocked shared sandbox could not be stopped",
                    extra={
                        "event": "sandbox_archive_blocked_stop_failed",
                        "sandbox_id": spec.sandbox_id,
                        "generation": spec.generation,
                    },
                )
            finally:
                with self._lock:
                    group.stopping = False
                    self._condition.notify_all()
                    found.extend(self._descriptors(group))
            return found

        # Docker stop, compression, and S3 upload may take minutes. The
        # per-group ``stopping`` barrier rejects competing activation while
        # allowing unrelated groups' status and lifecycle calls to proceed.
        if retry or finalize:
            try:
                if retry:
                    self._retry_stopped_archive(group)
                else:
                    if inspection.running and self._stopped_archive_prepare_hook is not None:
                        self._stopped_archive_prepare_hook(spec, inspection)
                    inspection = self._manager.stop(
                        spec,
                        expected_container_id=inspection.container_id,
                        release_networks=self._stopped_archive_hook is None,
                    )
                    inspection = self._persist_stopped_archive(group, inspection, members)
                    with self._lock:
                        group.orphan_finalization_pending = False
            except SandboxArchiveBlocked:
                with self._lock:
                    group.stop_archive_pending = True
                    group.stop_archive_blocked = True
                    group.stop_archive_members = members
                    group.orphan_finalization_pending = False
                try:
                    if inspection.running:
                        inspection = self._manager.stop(
                            spec,
                            expected_container_id=inspection.container_id,
                            release_networks=False,
                        )
                    with self._lock:
                        self._accept_inspection_locked(group, inspection)
                except Exception:
                    logger.exception(
                        "durably blocked sandbox could not be exact-stopped",
                        extra={
                            "event": "sandbox_archive_durable_block_stop_failed",
                            "sandbox_id": spec.sandbox_id,
                            "generation": spec.generation,
                        },
                    )
            except Exception as exc:
                # Durable marker decoding, an exact Docker stop, and the S3
                # operation can all fail independently. This orphan stays
                # fenced and is reclassified from its marker on the next
                # reconciliation, but it must not prevent later groups from
                # receiving their own lifecycle and network-guard work.
                with self._lock:
                    group.orphan_finalization_pending = True
                    group.orphan_marker_unreadable = self._stopped_archive_hook is not None
                if inspection.running:
                    try:
                        inspection = self._manager.stop(
                            spec,
                            expected_container_id=inspection.container_id,
                            release_networks=False,
                        )
                    except Exception:
                        logger.exception(
                            "failed-finalization sandbox could not be exact-stopped",
                            extra={
                                "event": "sandbox_archive_finalization_stop_failed",
                                "sandbox_id": spec.sandbox_id,
                                "generation": spec.generation,
                            },
                        )
                    else:
                        with self._lock:
                            self._accept_inspection_locked(group, inspection)
                logger.error(
                    "shared sandbox archive finalization remains pending",
                    extra={
                        "event": "sandbox_archive_finalization_pending",
                        "sandbox_id": spec.sandbox_id,
                        "generation": spec.generation,
                        "detail": _archive_failure_diagnostic("fail_stop_final", exc).render(),
                    },
                )
            finally:
                with self._lock:
                    group.stopping = False
                    self._condition.notify_all()
        with self._lock:
            found.extend(self._descriptors(group))
        return found

    def register_manifest(
        self,
        spec: ChatSandboxSpec,
        members: builtins.list[SharedMemberManifest],
        *,
        expected_container_id: str | None = None,
        facts: SandboxActionFacts | None = None,
        observer: StageRecorder | None = None,
    ) -> builtins.list[SharedSessionDescriptor]:
        """Install the control plane's authoritative member aliases."""
        if not members:
            raise ValueError("a shared sandbox manifest cannot be empty")
        seen_slots: set[int] = set()
        # The validation pass rejects a manifest with no resident slot before
        # anything is mutated, so the install pass below reads the slot from
        # this pair rather than re-deciding what the check above established.
        validated_members: builtins.list[tuple[SharedMemberManifest, int]] = []
        for manifest in members:
            if manifest.resident_slot_id is None:
                raise ValueError("every ready shared member needs a resident slot")
            if not 0 <= manifest.resident_slot_id < spec.resident_slot_capacity:
                raise ValueError("member resident slot is outside sandbox capacity")
            if manifest.resident_slot_id in seen_slots:
                raise ValueError("two shared members cannot own one resident slot")
            seen_slots.add(manifest.resident_slot_id)
            validated_members.append((manifest, manifest.resident_slot_id))
        with self._lock:
            deadline = time.monotonic() + 30.0
            group = self._groups.get(spec.sandbox_id)
            while group is None and spec.sandbox_id in self._creating:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionBusy("shared workspace creation is in progress")
                self._condition.wait(timeout=min(remaining, 0.5))
                group = self._groups.get(spec.sandbox_id)
            if group is not None:
                while group.lifecycle_check is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SessionBusy("shared sandbox lifecycle check is in progress")
                    self._condition.wait(timeout=min(remaining, 0.5))
                    if self._groups.get(spec.sandbox_id) is not group:
                        raise SessionLeaseLost(
                            "shared sandbox lifecycle changed during manifest admission"
                        )
                return self._install_manifest_locked(
                    group,
                    spec,
                    members,
                    validated_members,
                    expected_container_id=expected_container_id,
                )
            self._creating.add(spec.sandbox_id)
        # Docker create, or the exact adoption check, can take minutes. Only registrations of this sandbox wait for it; the
        # metadata lock stays free for every other group and member.
        try:
            created = (
                self._manager.require_existing(spec, container_id=expected_container_id)
                if expected_container_id
                else self._manager.create(spec, facts=facts, observer=observer)
            )
        except BaseException:
            with self._lock:
                self._creating.discard(spec.sandbox_id)
                self._condition.notify_all()
            raise
        with self._lock:
            self._creating.discard(spec.sandbox_id)
            self._condition.notify_all()
            group = self._groups.get(spec.sandbox_id)
            if group is None:
                group = _Group(spec=spec, inspection=created)
                self._groups[spec.sandbox_id] = group
            return self._install_manifest_locked(
                group,
                spec,
                members,
                validated_members,
                expected_container_id=expected_container_id,
            )

    def _install_manifest_locked(
        self,
        group: _Group,
        spec: ChatSandboxSpec,
        members: builtins.list[SharedMemberManifest],
        validated_members: builtins.list[tuple[SharedMemberManifest, int]],
        *,
        expected_container_id: str | None,
    ) -> builtins.list[SharedSessionDescriptor]:
        """Apply one validated manifest to its registered group, under the lock."""
        if group.spec != spec:
            raise SandboxError("shared sandbox immutable identity/configuration changed")
        if expected_container_id and group.inspection.container_id != expected_container_id:
            raise RetainedChatLost("shared sandbox container identity changed")
        if group.lifecycle_check is not None:
            raise SessionBusy("shared sandbox lifecycle check is in progress")
        if group.archive_request_id is not None:
            raise SessionBusy("shared sandbox archive and deletion is in progress")

        wanted = {item.thread_id for item in members}
        for manifest, resident_slot_id in validated_members:
            other_group = self._members.get(manifest.thread_id)
            if other_group is not None and other_group is not group:
                raise SandboxError("chat membership points at two sandbox groups")
            if manifest.thread_id != spec.thread_id and manifest.native_session_id is None:
                # Fork reserves a database membership before creating its
                # native child. Only the origin can start without native
                # context; installing the reservation here would make
                # fork_native reject its own target as already occupied.
                # A stale pending manifest must also preserve a child the
                # host has materialized but the database has not finalized.
                continue
            member = group.members.get(manifest.thread_id)
            if member is None:
                # A durable observation that the previous sandbox manager owned
                # a live process is not an attachable local transport. Docker
                # preserves the native session files, but the replacement
                # sandbox manager has no stdin/stdout pipes and must resume it
                # before claiming a HOT hit.
                resident_state: ResidentState = manifest.resident_state
                if resident_state == "live":
                    resident_state = "restarting" if manifest.native_session_id else "stopped"
                member = _Member(
                    thread_id=manifest.thread_id,
                    native_session_id=manifest.native_session_id,
                    context_status=manifest.context_status,
                    activation_generation=manifest.activation_generation,
                    process_generation=manifest.process_generation,
                    residency_generation=manifest.residency_generation,
                    resident_state=resident_state,
                    resident_slot_id=resident_slot_id,
                    last_settled_job_id=manifest.last_settled_job_id,
                )
                group.members[manifest.thread_id] = member
                self._members[manifest.thread_id] = group
            else:
                if member.resident_slot_id != manifest.resident_slot_id:
                    raise SandboxError("member resident slot changed in place")
                if member.lease_token is None and member.current_turn is None:
                    member.native_session_id = manifest.native_session_id
                    member.context_status = manifest.context_status
                    member.activation_generation = max(
                        member.activation_generation,
                        manifest.activation_generation,
                    )
                    member.process_generation = max(
                        member.process_generation,
                        manifest.process_generation,
                    )
                    # A durable generation is not proof that the attached
                    # process was launched with that generation's secret.
                    # Advance it only when there is no local pipe; with a
                    # live transport, leaving the old value makes
                    # ``_ensure_transport`` perform the required re-keyed
                    # native restart.
                    if member.transport is None:
                        member.residency_generation = max(
                            member.residency_generation,
                            manifest.residency_generation,
                        )
                    member.last_settled_job_id = manifest.last_settled_job_id
                    if member.transport is None:
                        member.resident_state = manifest.resident_state
                        if member.resident_state == "live":
                            member.resident_state = (
                                "restarting" if member.native_session_id else "stopped"
                            )
        # Removal is safe only for a manifest member with no live local
        # ownership. A stale manifest never tears down an active process.
        for thread_id, member in list(group.members.items()):
            if thread_id in wanted or member.lease_token or member.transport:
                continue
            group.members.pop(thread_id, None)
            self._members.pop(thread_id, None)
        return self._descriptors(group)

    def owns(self, thread_id: str) -> bool:
        """Whether this supervisor currently knows the membership alias."""
        with self._lock:
            return thread_id in self._members

    def admin_deletion_target_absent(
        self,
        thread_id: str,
        *,
        sandbox_id: str,
        container_id: str,
    ) -> bool:
        """Prove a stale durable sandbox has no corresponding Docker object.

        This is only the idempotent terminal case for an already-fenced admin
        discard. A known membership, the original container under another
        name, or any object occupying the group's stable name all mean the
        target is not absent. Docker/protocol failures propagate and therefore
        cannot be mistaken for successful deletion.
        """
        with self._lock:
            # A registration creating or adopting the sandbox does so outside
            # the lock and installs the group once Docker answers; until then
            # only ``_creating`` says it is not absent.
            if (
                thread_id in self._members
                or sandbox_id in self._groups
                or sandbox_id in self._creating
            ):
                return False
        container_name = ChatSandboxSpec.container_name_for(thread_id, sandbox_id)
        if self._manager.inspect(container_name) is not None:
            return False
        absent = self._manager.inspect(container_id) is None
        with self._lock:
            return absent and not (
                thread_id in self._members
                or sandbox_id in self._groups
                or sandbox_id in self._creating
            )

    def acquire(
        self,
        spec: ChatSandboxSpec,
        *,
        thread_id: str,
        owner: str,
        activation_generation: int,
        has_settled_context: bool,
        manifest: builtins.list[SharedMemberManifest],
        expected_container_id: str | None = None,
        profiling: dict[str, Any] | None = None,
    ) -> SharedSessionLease:
        """Start one group if needed and lease only the addressed member."""
        # Container actions are always reported: they are a couple of string
        # assignments, and the runner needs them whenever it is profiling.
        # Durations are collected only for a selection this process can honour:
        # its own switch, its own run, and the caller's inherited deadline.
        facts = SandboxActionFacts()
        recorder = start_stage_recording(profiling)
        call_id = context_call_id(profiling)
        self.register_manifest(
            spec,
            manifest,
            expected_container_id=expected_container_id,
            facts=facts,
            observer=recorder,
        )
        with self._lock:
            group, member = self._member(thread_id)
            transition_deadline = time.monotonic() + 30.0
            while group.snapshotting or group.starting or group.stopping:
                remaining = transition_deadline - time.monotonic()
                if remaining <= 0:
                    transition = (
                        "snapshot"
                        if group.snapshotting
                        else ("start" if group.starting else "stop")
                    )
                    raise SessionBusy(f"shared workspace {transition} is in progress")
                self._condition.wait(timeout=min(remaining, 0.5))
            if group.stop_archive_pending:
                raise SessionBusy("shared workspace final archive retry is pending")
            if group.orphan_finalization_pending:
                raise SessionBusy("shared workspace orphan finalization is pending")
            if member.lease_token is not None:
                if member.lease_owner == owner:
                    return self._lease(
                        group, member, facts=facts, recorder=recorder, call_id=call_id
                    )
                raise SessionBusy(f"chat {thread_id!r} already has an active turn")
            if member.current_turn is not None:
                raise SessionBusy(f"chat {thread_id!r} already has a running turn")
            # Fence stale attempt owners before doing anything observable to
            # the shared container. In particular, an old lease must not wake
            # a stopped workspace only to be rejected after ``docker start``.
            if activation_generation < member.activation_generation:
                raise SessionLeaseLost("member activation generation is stale")
            lifecycle_generation = group.lifecycle_generation
        # Docker answers outside the metadata lock. The start below and the
        # admission after it check again everything this observation stood for.
        current = self._manager.inspect(group.spec.container_name)
        if current is None or (
            expected_container_id and current.container_id != expected_container_id
        ):
            with self._lock:
                if self._members.get(thread_id) is group and group.members.get(thread_id) is member:
                    member.context_status = "lost"
                    member.resident_state = "lost"
            if current is None:
                raise RetainedChatLost("shared sandbox container is missing")
            raise RetainedChatLost("shared sandbox container identity changed")

        inspection = self._start_group(
            group,
            facts=facts,
            observer=recorder,
        )
        with self._lock:
            current_group = self._members.get(thread_id)
            current_member = (
                current_group.members.get(thread_id) if current_group is not None else None
            )
            if (
                current_group is not group
                or current_member is not member
                or group.lifecycle_generation != lifecycle_generation
            ):
                raise SessionLeaseLost("shared sandbox lifecycle changed during activation")
            self._require_mutation_admission(group)
            if group.stop_archive_pending:
                raise SessionBusy("shared workspace final archive retry is pending")
            if member.lease_token is not None:
                if member.lease_owner == owner:
                    return self._lease(
                        group, member, facts=facts, recorder=recorder, call_id=call_id
                    )
                raise SessionBusy(f"chat {thread_id!r} already has an active turn")
            if member.current_turn is not None:
                raise SessionBusy(f"chat {thread_id!r} already has a running turn")
            if activation_generation < member.activation_generation:
                raise SessionLeaseLost("member activation generation is stale")
            if expected_container_id and inspection.container_id != expected_container_id:
                member.context_status = "lost"
                member.resident_state = "lost"
                raise RetainedChatLost("shared sandbox container identity changed")
            group.inspection = inspection
            member.activation_generation = activation_generation
            member.settlement_receipt = None
            member.lease_token = secrets.token_urlsafe(32)
            member.lease_owner = owner
            hot_transport = member.transport is not None and member.transport.alive
            member.context_status = (
                "live"
                if has_settled_context and hot_transport
                else ("restored" if has_settled_context else "new")
            )
            group.idle_deadline = None
            return self._lease(
                group,
                member,
                facts=facts,
                recorder=recorder,
                call_id=call_id,
                network_verified=True,
            )

    def ensure_resident(
        self,
        thread_id: str,
        token: str,
        launch: ResidentLaunch,
        profiling: dict[str, Any] | None = None,
    ) -> SharedSessionDescriptor:
        """Open or reuse the member transport after workspace preparation."""
        # Only durations are gated: the member's action is reported either way,
        # because the runner needs it to classify the startup path.
        recorder = start_stage_recording(profiling)
        call_id = context_call_id(profiling)
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            self._wait_for_start_locked(group)
            group, member = self._leased_member(thread_id, token)
            self._require_mutation_admission(group)
        # CLI initialization may block. The member lock, not the group lock,
        # leaves siblings able to initialize and execute simultaneously. The
        # lease is checked again around the launch: an activation abandoned
        # while it waited or launched must not install its process.
        with member.turn_lock:
            resident_action = self._ensure_transport(
                group, member, launch, observer=recorder, lease_token=token
            )
        with self._lock:
            return self._descriptor(
                group,
                member,
                resident_action=resident_action,
                stages=recorder.stages if recorder is not None else [],
                call_id=call_id,
                recorded=recorder is not None,
                truncated=recorder is not None and recorder.stopped_early(),
            )

    def start_turn(
        self,
        thread_id: str,
        token: str,
        *,
        prompt: str,
        model: str,
        reasoning_effort: str | None,
    ) -> ResidentTurn[DialectRuntime]:
        """Send a prompt to one member without taking a group-wide turn lock."""
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            self._wait_for_start_locked(group)
            group, member = self._leased_member(thread_id, token)
            self._require_mutation_admission(group)
            if member.current_turn is not None:
                raise SessionBusy(f"chat {thread_id!r} already has a running turn")
            transport = member.transport
            if transport is None or not transport.alive:
                member.resident_state = "restarting"
                raise SandboxError("member resident transport is not live")
            lifecycle_generation = group.lifecycle_generation
            group.idle_deadline = None
        if not member.turn_lock.acquire(blocking=False):
            raise SessionBusy(f"chat {thread_id!r} already has a running turn")
        observer_cursor: GuestObserverCursor | None = None
        window: MemberWindowHandle | None = None
        window_sampler: MemberWindowSampler | None = None
        try:
            activation_processes = self._manager.capture_member_processes(
                group.spec,
                slot_id=member.resident_slot_id,
                user_uid=MEMBER_UID_BASE + member.resident_slot_id,
                user_gid=self._manager.backend.gid,
            )
            mark_reader_factory = getattr(self._manager, "tool_mark_reader", None)
            tool_mark_reader = (
                mark_reader_factory(group.spec)
                if (
                    mark_reader_factory is not None
                    and transport.launch.runtime_id == "claude-code"
                    and self._manager.backend.runtime != KATA_RUNTIME
                )
                else None
            )
            if self._manager.backend.runtime == KATA_RUNTIME:
                begin_observation = getattr(self._manager, "begin_guest_observation", None)
                if begin_observation is None:
                    observer_cursor = GuestObserverCursor.unavailable(
                        MEMBER_UID_BASE + member.resident_slot_id,
                        "sandbox manager has no guest observer client",
                    )
                else:
                    # The window opens on the same fence as the turn: the
                    # measurement identifier travels with the cursor so the
                    # observation's journal slice contains the opening census
                    # as well as everything after it.
                    measurement_id = uuid.uuid4().hex
                    observer_cursor = begin_observation(
                        group.spec,
                        user_uid=MEMBER_UID_BASE + member.resident_slot_id,
                        measurement_id=measurement_id,
                    )
                    if observer_cursor.available:
                        window_sampler = MemberWindowSampler(
                            run=lambda argv, timeout_s: self._manager.member_window_request(
                                group.spec,
                                argv,
                                timeout_s=timeout_s,
                            ),
                            interval_s=self._sample_interval_s,
                        )
                        window = window_sampler.open(
                            uid=MEMBER_UID_BASE + member.resident_slot_id,
                            measurement_id=measurement_id,
                            cursor=observer_cursor,
                        )
                        window = replace(
                            window,
                            cpu_capacity_cores=parse_cpu_capacity(group.spec.cpu_limit),
                        )
            with self._lock:
                self._wait_for_start_locked(group)
                self._require_mutation_admission(group)
                current_group = self._members.get(thread_id)
                current_member = (
                    current_group.members.get(thread_id) if current_group is not None else None
                )
                if (
                    current_group is not group
                    or current_member is not member
                    or group.stopping
                    or group.lifecycle_generation != lifecycle_generation
                    or member.lease_token != token
                    or member.transport is not transport
                ):
                    raise SessionLeaseLost("shared sandbox lifecycle changed before turn admission")
            turn = transport.start_turn(
                prompt=prompt,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except Exception:
            if window is not None and window_sampler is not None:
                with contextlib.suppress(Exception):
                    window_sampler.close(window)
            if observer_cursor is not None:
                with contextlib.suppress(Exception):
                    self._manager.finish_guest_observation(group.spec, observer_cursor)
            member.turn_lock.release()
            raise
        admitted = False
        held_inputs: list[PendingInput] = []
        with self._lock:
            current_group = self._members.get(thread_id)
            current_member = (
                current_group.members.get(thread_id) if current_group is not None else None
            )
            admitted = (
                current_group is group
                and current_member is member
                and not group.stopping
                and group.lifecycle_generation == lifecycle_generation
                and member.lease_token == token
                and member.transport is transport
            )
            if admitted:
                member.current_turn = turn
                held_inputs = self._take_held_inputs(thread_id, member.lease_owner)
                member.activation_processes = activation_processes
                member.observer_cursor = observer_cursor
                member.tool_mark_reader = tool_mark_reader
                member.window = window
                member.window_sampler = window_sampler
                member.window_sequence = 0
                member.window_accumulator = (
                    WindowAccumulator(
                        uid=window.uid,
                        measurement_id=window.measurement_id,
                        opened_guest_mono_ns=(getattr(observer_cursor, "guest_mono_ns", 0) or 0),
                        boot_id=self._window_boot_id(group.spec, observer_cursor),
                    )
                    if window is not None
                    else None
                )
                member.window_next_sample_mono = 0.0
                member.window_checkpoints = (
                    CheckpointSchedule(interval_s=self._checkpoint_interval_s)
                    if window is not None
                    else None
                )
        if not admitted:
            with contextlib.suppress(Exception):
                turn.interrupt()
            if observer_cursor is not None:
                with contextlib.suppress(Exception):
                    self._manager.finish_guest_observation(group.spec, observer_cursor)
            with contextlib.suppress(Exception):
                self._manager.reap_member_processes_except(
                    group.spec,
                    slot_id=member.resident_slot_id,
                    user_uid=MEMBER_UID_BASE + member.resident_slot_id,
                    user_gid=self._manager.backend.gid,
                    baseline=activation_processes,
                )
            member.turn_lock.release()
            raise SessionLeaseLost("shared sandbox lifecycle changed during turn admission")
        for pending in held_inputs:
            turn.inject(pending.text, message_id=pending.message_id)
        return turn

    def _take_held_inputs(self, thread_id: str, owner: str | None) -> list[PendingInput]:
        """Remove and return what was held for ``owner``'s turn; drop the rest."""
        now = self._clock()
        return [
            held.pending
            for held in self._held_inputs.pop(thread_id, [])
            if held.owner == owner and now - held.held_at < HELD_INPUT_TTL_S
        ]

    def complete_turn(
        self,
        thread_id: str,
        token: str,
        turn: ResidentTurn[DialectRuntime],
    ) -> SharedTurnTelemetry:
        """Close observation, then enforce and release the activation boundary."""
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            if member.current_turn is not turn:
                raise SessionLeaseLost("member turn generation is stale")
            slot_id = member.resident_slot_id
            activation_processes = member.activation_processes
            observer_cursor = member.observer_cursor
            tool_mark_reader = member.tool_mark_reader
            window = member.window
            window_sampler = member.window_sampler
            window_sequence = member.window_sequence
        # The closing census is taken while the window is still open and
        # before the observation is read, so the last cycle lands inside the
        # journal slice the observation is about to freeze. Sampling after
        # that would measure activity the slice does not contain.
        if window is not None and window_sampler is not None:
            window_sampler.sample(window, window_sequence + 1)
        observation = GuestObservation.unavailable("sandbox is not a Kata guest")
        if self._manager.backend.runtime == KATA_RUNTIME:
            finish_observation = getattr(self._manager, "finish_guest_observation", None)
            if finish_observation is None:
                observation = GuestObservation.unavailable(
                    "sandbox manager has no guest observer client",
                    source=GUEST_OBSERVER_SOURCE,
                )
            else:
                # Close before the member reaper/permission normalizer below:
                # those are platform processes and must not be billed to the
                # tool call whose background children they clean up.
                try:
                    finished = finish_observation(group.spec, observer_cursor)
                    if isinstance(finished, GuestObservation):
                        observation = (
                            finished
                            if finished.source is not None
                            else replace(finished, source=GUEST_OBSERVER_SOURCE)
                        )
                    else:
                        observation = GuestObservation.unavailable(
                            "sandbox manager returned invalid process telemetry",
                            source=GUEST_OBSERVER_SOURCE,
                        )
                except Exception as exc:
                    observation = GuestObservation.unavailable(
                        str(exc),
                        source=GUEST_OBSERVER_SOURCE,
                    )
        if window is None:
            final_sequence = window_sequence + 1
        else:
            with self._lock:
                _, live_member = self._leased_member(thread_id, token)
                live_member.window_report_seq += 1
                final_sequence = live_member.window_report_seq
        turn_resources = self._freeze_window(
            window=window,
            window_sampler=window_sampler,
            observation=observation,
            sequence=final_sequence,
        )
        telemetry = SharedTurnTelemetry(
            observation=observation,
            tool_marks=(
                tuple(tool_mark_reader.read() if tool_mark_reader is not None else ())
                + observation.tool_marks
            ),
            turn_resources=turn_resources,
        )
        boundary_error: Exception | None = None
        try:
            if activation_processes is None:
                raise SandboxError("member activation process baseline is missing")
            self._manager.reap_member_processes_except(
                group.spec,
                slot_id=slot_id,
                user_uid=MEMBER_UID_BASE + slot_id,
                user_gid=self._manager.backend.gid,
                baseline=activation_processes,
            )
            self._manager.normalize_member_files(
                group.spec,
                slot_id=slot_id,
                user_uid=MEMBER_UID_BASE + slot_id,
                user_gid=self._manager.backend.gid,
            )
        except Exception as exc:  # the caller must fail this settlement closed
            boundary_error = exc
            logger.warning(
                "shared member activation boundary failed for %s: %s",
                thread_id,
                exc,
                extra={
                    "event": "shared_member_activation_boundary_failed",
                    "thread_id": thread_id,
                    "resident_slot_id": slot_id,
                },
                exc_info=True,
            )
        detached = False
        try:
            with self._lock:
                _, member = self._leased_member(thread_id, token)
                if member.current_turn is not turn:
                    raise SessionLeaseLost("member turn generation is stale")
                # Detaching the turn hands its member lock to this call, which
                # releases it however the bookkeeping below ends.
                member.current_turn = None
                detached = True
                member.activation_processes = None
                member.observer_cursor = None
                member.tool_mark_reader = None
                member.window = None
                member.window_sampler = None
                member.window_checkpoints = None
                member.window_accumulator = None
                transport = member.transport
                if boundary_error is not None:
                    member.transport = None
                    member.resident_state = "restarting"
                elif transport is None or not transport.alive:
                    member.resident_state = "restarting"
                    logger.warning(
                        "shared member resident transport exited across activation boundary",
                        extra={
                            "event": "shared_member_transport_exited",
                            "thread_id": thread_id,
                            "resident_slot_id": slot_id,
                            "activation_processes": activation_processes,
                        },
                    )
                else:
                    member.resident_state = "live"
        finally:
            if detached:
                member.turn_lock.release()
        if boundary_error is not None:
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
            raise SharedTurnBoundaryError(
                f"shared member activation boundary failed: {boundary_error}",
                telemetry,
            ) from boundary_error
        return telemetry

    def _window_boot_id(self, spec: Any, cursor: Any) -> str:
        """The boot every instance key in one window is stamped with.

        The cursor carries it when the observer could report one. Falling back
        to asking the manager keeps a window whose cursor predates that field --
        or a guest that could not answer -- from keying every thread under an
        empty boot, which would let two boots' task numbers collide.
        """
        from_cursor = getattr(cursor, "guest_boot_id", "") or ""
        if from_cursor:
            return from_cursor
        read = getattr(self._manager, "guest_boot_id", None)
        if read is None:
            return ""
        try:
            return read(spec) or ""
        except Exception:
            return ""

    def _freeze_window(
        self,
        *,
        window: MemberWindowHandle | None,
        window_sampler: MemberWindowSampler | None,
        observation: GuestObservation,
        sequence: int,
    ) -> dict[str, Any] | None:
        """Close the member window and reduce it into the reported snapshot.

        Every failure here degrades to "not measured" rather than raising: a
        turn whose resources could not be counted is still a turn that ran,
        and observation must never be able to fail the user's work.
        """
        if window is None or window_sampler is None:
            return None
        if not window.baseline_taken:
            # The opening census never landed, so this window has no baseline.
            # Reducing it would report the member's whole session as this
            # turn's work, which is exactly the attribution this design
            # refuses to make.
            with contextlib.suppress(Exception):
                window_sampler.close(window)
            return None
        close_started = time.monotonic()
        with contextlib.suppress(Exception):
            window_sampler.close(window)
        close_scan_ms = max(0, int((time.monotonic() - close_started) * 1000))
        cursor = window.cursor
        duration_ms = max(0, int((time.monotonic() - window.opened_host_mono) * 1000))
        try:
            snapshot = reduce_window(
                uid=window.uid,
                measurement_id=window.measurement_id,
                boot_id=getattr(cursor, "guest_boot_id", "") or "",
                samples=observation.window_samples,
                journal_payload=observation.journal_payload,
                opened_guest_mono_ns=getattr(cursor, "guest_mono_ns", 0) or 0,
                lost_events=observation.lost_events,
                state="final",
                duration_ms=duration_ms,
                report_seq=sequence,
                open_scan_ms=window.open_scan_ms,
                close_scan_ms=close_scan_ms,
            )
            snapshot["cpu"]["capacity_cores"] = window.cpu_capacity_cores
            return snapshot
        except Exception:
            logger.warning(
                "member window reduction failed for %s",
                window.measurement_id,
                extra={
                    "event": "shared_member_window_reduce_failed",
                    "measurement_id": window.measurement_id,
                },
                exc_info=True,
            )
            return None

    def sample_open_windows(self) -> int:
        """Take one census for every member window that is due for one.

        The census has its own clock on purpose. Driving it from the turn's
        event stream meant it ran only when the agent happened to emit
        something, so a turn that spent its time waiting on a model produced
        almost no cycles, and the per-instance deltas and the PSS peak were
        computed from a handful of readings while the summary still advertised
        a one-second period.

        Returns how many windows were sampled. A census that fails is not
        retried here: the next tick is a second away and a late census no
        longer describes the moment it was asked about.
        """
        sampled = 0
        now = time.monotonic()
        with self._lock:
            members = [
                member
                for group in self._groups.values()
                for member in group.members.values()
                if member.window is not None
                and member.window_sampler is not None
                and member.window.baseline_taken
            ]
            due: builtins.list[DueCensus] = []
            for member in members:
                window = member.window
                sampler = member.window_sampler
                # The comprehension above already dropped a member without
                # both. Re-reading them into locals is what carries that fact
                # into the hand-off, so the census loop below works on a
                # handle and a sampler rather than on two values a reader has
                # to go back and prove are there.
                if window is None or sampler is None:
                    continue
                if now < member.window_next_sample_mono:
                    continue
                member.window_next_sample_mono = now + sampler.interval_s
                member.window_sequence += 1
                due.append(
                    (
                        window,
                        sampler,
                        member.window_sequence,
                        member.observer_cursor,
                        member.window_accumulator,
                    )
                )
        for window, sampler, sequence, cursor, accumulator in due:
            with contextlib.suppress(Exception):
                sampler.sample(window, sequence)
            # Fold in whatever the census just wrote, so the running state and
            # any checkpoint built from it describe real cycles rather than an
            # empty reducer.
            if accumulator is not None and cursor is not None:
                with contextlib.suppress(Exception):
                    self._consume_window_journal(accumulator, cursor)
            sampled += 1
        # Reporting runs on the same clock as the census rather than on the
        # turn's event stream. A turn that spends a minute inside one silent
        # tool call emits nothing for that minute, and an upload that waits for
        # an event would leave nothing behind if the host died in it.
        self._report_due_checkpoints()
        return sampled

    def take_reported_checkpoint(self, thread_id: str) -> dict[str, Any] | None:
        """The newest checkpoint not yet handed to a caller, or None.

        The sampler thread produces checkpoints on its own clock; this is how
        the turn's event stream collects them. Taking is destructive so a
        checkpoint is forwarded once, but the window keeps producing new ones
        whether or not anything ever asks.
        """
        with self._lock:
            try:
                _, member = self._leased_member(thread_id, self._lease_token_for(thread_id))
            except Exception:
                return None
            pending = member.window_pending_checkpoint
            member.window_pending_checkpoint = None
            return pending

    def _lease_token_for(self, thread_id: str) -> str:
        with self._lock:
            group = self._members.get(thread_id)
            if group is None:
                raise SessionLeaseLost("member is not in a shared sandbox")
            return group.members[thread_id].lease_token or ""

    def _report_due_checkpoints(self) -> None:
        """Produce a checkpoint for every window whose interval has elapsed.

        The checkpoint is parked on the member for whoever forwards it, and the
        optional hook is an extra observer rather than the delivery mechanism.
        Gating this on a hook meant that a supervisor built without one -- which
        is how the sandbox manager builds it -- produced no checkpoints at all, so a
        crashed turn kept nothing.
        """
        with self._lock:
            due = [
                (member.thread_id, member.lease_token)
                for group in self._groups.values()
                for member in group.members.values()
                if member.window is not None
                and member.window_checkpoints is not None
                and member.lease_token is not None
                and member.window_checkpoints.due()
            ]
        for thread_id, token in due:
            with contextlib.suppress(Exception):
                snapshot = self.checkpoint_running_window(thread_id, token)
                if snapshot is None:
                    continue
                if self._window_checkpoint_hook is not None:
                    with contextlib.suppress(Exception):
                        self._window_checkpoint_hook(thread_id, snapshot)

    def _consume_window_journal(
        self,
        accumulator: WindowAccumulator,
        cursor: GuestObserverCursor,
    ) -> int:
        """Fold this window's journal, read so far, into its accumulator.

        The slice read is the new tail only -- reading the whole journal once a
        second for the length of a turn would be wasteful -- but it is handed
        on top of what was read before, because identity has to be derived from
        the whole history rather than from whichever slice a record landed in.

        Read errors are swallowed by the caller: a census the host could not
        read back is a gap in the checkpoint, and the next tick tries again
        from the same offset rather than skipping the bytes it never saw.
        """
        read = getattr(self._manager, "member_window_journal_delta", None)
        if read is None:
            return 0
        member = self._member_with_accumulator(accumulator)
        if member is None:
            return 0
        scoped = replace(cursor, offset=member.window_journal_offset)
        payload, new_offset = read(self._group_spec_for(accumulator), scoped)
        # A journal read can catch the observer mid-write, and the tail of such
        # a read is half a record. Advancing past it would lose that cycle for
        # good and leave nothing in the accounting to say so, so the offset
        # stops at the last complete line and the partial tail is read again
        # once the writer has finished it.
        complete = payload.rfind(b"\n") + 1
        member.window_journal_offset = new_offset - (len(payload) - complete)
        return accumulator.consume(
            payload[:complete],
            boot_id=cursor.guest_boot_id,
            upto_offset=member.window_journal_offset,
        )

    def _member_with_accumulator(self, accumulator: WindowAccumulator) -> Any | None:
        """The member owning one accumulator, or None once its window closed.

        One accumulator belongs to exactly one member at a time, so this takes
        the single owner rather than iterating: a loop that returned on its
        first pass would silently skip a second owner if that ever changed.
        """
        with self._lock:
            for group in self._groups.values():
                for member in group.members.values():
                    if member.window_accumulator is accumulator:
                        return member
        return None

    def _group_spec_for(self, accumulator: WindowAccumulator) -> Any:
        """The sandbox spec owning one window's accumulator."""
        with self._lock:
            for group in self._groups.values():
                for member in group.members.values():
                    if member.window_accumulator is accumulator:
                        return group.spec
        raise SandboxError("member window accumulator has no sandbox")

    def _sample_loop(self) -> None:
        """Census every open window once a second until the supervisor stops."""
        while not self._stop.wait(self._sample_tick_s):
            with contextlib.suppress(Exception):
                self.sample_open_windows()

    def checkpoint_running_window(
        self,
        thread_id: str,
        token: str,
    ) -> dict[str, Any] | None:
        """Build one cumulative checkpoint for a member's running turn.

        Returns the snapshot to report, or None when no checkpoint is due or
        the member has no open window. The schedule owns the cadence: a caller
        may invoke this on any poll and only the fifteenth second produces a
        report. The censuses themselves are taken by
        :meth:`sample_open_windows` on their own clock, so this only decides
        when to upload.

        The snapshot carries the counters this window has been credited with
        so far. It is built from the member's running reducer rather than a
        fresh one, so a checkpoint describes the same measurement the final
        report will, with the window still open.
        """
        with self._lock:
            try:
                _, member = self._leased_member(thread_id, token)
            except Exception:
                return None
            window = member.window
            window_sampler = member.window_sampler
            schedule = member.window_checkpoints
            cursor = member.observer_cursor
            if window is None or window_sampler is None or schedule is None:
                return None
            if not window.baseline_taken or not schedule.due():
                return None
            schedule.mark_sent()
            member.window_report_seq += 1
            report_seq = member.window_report_seq
            boot_id = getattr(cursor, "guest_boot_id", "") or ""
            opened = getattr(cursor, "guest_mono_ns", 0) or 0
            accumulator = member.window_accumulator
        try:
            duration_ms = max(0, int((time.monotonic() - window.opened_host_mono) * 1000))
            if accumulator is not None:
                # The same counters the final report will carry, with the
                # window still open: a crash keeps everything measured so far.
                #
                # Without a cursor there is no journal fence to read from, so
                # the checkpoint reports what the accumulator already holds.
                # Calling through anyway raised inside the suppression below,
                # which read as a fold that happened rather than one that was
                # never possible.
                if cursor is not None:
                    with contextlib.suppress(Exception):
                        self._consume_window_journal(accumulator, cursor)
                snapshot = accumulator.snapshot(
                    state="collecting",
                    duration_ms=duration_ms,
                    report_seq=report_seq,
                    open_scan_ms=window.open_scan_ms,
                )
            else:
                snapshot = reduce_window(
                    uid=window.uid,
                    measurement_id=window.measurement_id,
                    boot_id=boot_id,
                    samples=(),
                    opened_guest_mono_ns=opened,
                    state="collecting",
                    duration_ms=duration_ms,
                    report_seq=report_seq,
                    open_scan_ms=window.open_scan_ms,
                )
            snapshot["cpu"]["capacity_cores"] = window.cpu_capacity_cores
            with self._lock:
                group = self._members.get(thread_id)
                if group is not None and thread_id in group.members:
                    group.members[thread_id].window_pending_checkpoint = snapshot
        except Exception:
            logger.warning(
                "member window checkpoint failed for %s",
                window.measurement_id,
                extra={
                    "event": "shared_member_window_checkpoint_failed",
                    "measurement_id": window.measurement_id,
                },
                exc_info=True,
            )
            return None
        return snapshot

    def interrupt(self, thread_id: str, token: str) -> None:
        """Interrupt the exact member activation, leaving siblings untouched."""
        with self._lock:
            _, member = self._leased_member(thread_id, token)
            turn = member.current_turn
        if turn is not None:
            turn.interrupt()

    def inject_input(
        self, thread_id: str, *, owner: str, message_id: str, text: str
    ) -> InputOutcome:
        """Hand the owner's message to this member's turn, running or about to.

        ``owner`` is the lease owner the control plane expects — ``attempt:<id>``
        — so a message meant for one attempt never reaches a later one that
        re-leased the member. The runner's lease token is not needed: this is
        the control plane's request, authenticated by the sandbox manager, and the owner
        string is its fence. Siblings are untouched. A turn that has not
        started here yet (its sandbox is still being prepared) is given the
        message when it starts; one that never does leaves it to the control
        plane, which sends it as the next turn.
        """
        with self._lock:
            group = self._members.get(thread_id)
            member = group.members.get(thread_id) if group is not None else None
            turn = (
                member.current_turn if member is not None and member.lease_owner == owner else None
            )
            if turn is None:
                now = self._clock()
                held = [
                    entry
                    for entry in self._held_inputs.get(thread_id, [])
                    if now - entry.held_at < HELD_INPUT_TTL_S
                ]
                if all(entry.pending.message_id != message_id for entry in held):
                    held.append(_HeldInput(owner, PendingInput(message_id, text), now))
                self._held_inputs[thread_id] = held
                return InputOutcome(True)
        return turn.inject(text, message_id=message_id)

    def _question_turn(self, thread_id: str, owner: str) -> ResidentTurn[DialectRuntime] | None:
        """Find the owner's active turn while the lifecycle lock is held."""
        group = self._members.get(thread_id)
        member = group.members.get(thread_id) if group is not None else None
        if (
            group is None
            or group.stopping
            or member is None
            or not member.lease_token
            or member.lease_owner != owner
            or not owner.startswith("attempt:")
            or not owner.removeprefix("attempt:")
            or member.transport is None
            or not member.transport.alive
        ):
            return None
        return member.current_turn

    def pending_questions(self, thread_id: str, *, owner: str) -> builtins.list[dict[str, Any]]:
        """Read only questions belonging to the control plane's active attempt."""
        with self._lock:
            turn = self._question_turn(thread_id, owner)
            return [] if turn is None else turn.pending_questions()

    def answer_question(
        self,
        thread_id: str,
        *,
        owner: str,
        request_id: str,
        answers: dict[str, builtins.list[str]],
        reject: bool = False,
    ) -> InputOutcome:
        """Answer the owner's current turn; never retain replies for another turn.

        Keep the lifecycle lock through delivery so settlement, expiry, or a
        replacement activation cannot invalidate the owner check before the
        native transport sees the response. The driver's request id check
        fences questions that have already completed within that same turn.
        """
        with self._lock:
            turn = self._question_turn(thread_id, owner)
            if turn is None:
                return InputOutcome(False, INPUT_TURN_ENDED)
            return turn.answer_question(request_id, answers, reject=reject)

    def abort_turn(
        self,
        thread_id: str,
        token: str,
        turn: ResidentTurn[DialectRuntime],
    ) -> SharedTurnTelemetry:
        """Fence a turn whose sandbox manager stream disappeared before its result.

        An HTTP client can vanish while the CLI is quiet. Merely closing the
        response generator would otherwise leave both the native turn and the
        member lock alive forever. Interrupt first, then close the member's
        transport as the hard boundary before releasing the turn marker. Only
        this member is affected; sibling processes in the workspace continue.
        """
        with self._lock:
            _, member = self._leased_member(thread_id, token)
            if member.current_turn is not turn:
                raise SessionLeaseLost("member turn generation is stale")
            transport = member.transport
        with contextlib.suppress(Exception):
            turn.interrupt()
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        return self.complete_turn(thread_id, token, turn)

    def _replayed_settlement(
        self,
        member: _Member,
        token: str,
        *,
        job_id: str,
        context_status: ContextStatus,
        native_session_id: str | None,
    ) -> SharedSessionDescriptor | None:
        """Return the last acknowledged boundary when this exact request is replayed.

        Only the most recent completed activation can replay, and only while no
        successor owns the member. Every field the caller fenced on must still
        match, so a replayed acknowledgement can never describe a different
        activation, job, context or native session.
        """
        receipt = member.settlement_receipt
        if (
            receipt is None
            or not secrets.compare_digest(receipt[0], token)
            or receipt[1].last_settled_job_id != job_id
            or receipt[1].context_status != context_status
            or member.lease_token is not None
            or member.current_turn is not None
            or member.context_status == "lost"
            or member.last_settled_job_id != job_id
            or member.activation_generation != receipt[1].activation_generation
            or member.native_session_id != receipt[1].native_session_id
            or (native_session_id is not None and native_session_id != receipt[1].native_session_id)
        ):
            return None
        return receipt[1]

    def settle(
        self,
        thread_id: str,
        token: str,
        *,
        job_id: str,
        context_status: ContextStatus,
        native_session_id: str | None = None,
    ) -> SharedSessionDescriptor:
        """Settle one member while retaining its healthy live process."""
        with self._lock:
            group, member = self._member(thread_id)
            replay = self._replayed_settlement(
                member,
                token,
                job_id=job_id,
                context_status=context_status,
                native_session_id=native_session_id,
            )
            if replay is not None:
                return replay
            group, member = self._leased_member(thread_id, token)
            if member.current_turn is not None:
                raise SessionBusy("cannot settle a member while its turn is running")
            if member.transport is None or not member.transport.alive:
                diagnostic = "" if member.transport is None else member.transport.stderr_text()
                exit_code = None if member.transport is None else member.transport.exit_code
                logger.warning(
                    "shared member resident transport is unavailable before settlement "
                    "(exit_code=%s)",
                    exit_code,
                    extra={
                        "event": "shared_member_transport_unavailable_before_settlement",
                        "thread_id": thread_id,
                        "resident_slot_id": member.resident_slot_id,
                    },
                )
                if diagnostic:
                    logger.warning(
                        "shared member resident transport exited before settlement: %s",
                        diagnostic,
                        extra={
                            "event": "shared_member_transport_exited_before_settlement",
                            "thread_id": thread_id,
                            "resident_slot_id": member.resident_slot_id,
                        },
                    )
                raise SandboxError("cannot settle without a live resident transport")
            observed = member.transport.native_session_id
            if native_session_id and observed and observed != native_session_id:
                raise SandboxError("resident transport reported a different native session id")
            settled_session_id = observed or native_session_id
            if not settled_session_id:
                raise SandboxError("resident transport did not report a native session id")
            if member.native_session_id and settled_session_id != member.native_session_id:
                raise SandboxError("resident transport settled a different native session")
            member.native_session_id = settled_session_id
            member.last_settled_job_id = job_id
            member.context_status = context_status
            # Preserve the observed native locator before gracefully retiring
            # a retry-affected CLI. Unexpected process death still fails the
            # live-transport check above, and no subsequent turn can enter
            # until EOF has flushed the native history.
            if member.transport.retire_after_turn:
                member.transport.close()
                member.resident_state = "restarting"
            else:
                member.resident_state = "live"
            member.lease_token = None
            member.lease_owner = None
            self._arm_idle_if_quiescent(group)
            descriptor = self._descriptor(group, member)
            member.settlement_receipt = (token, descriptor)
            return descriptor

    def abandon(self, thread_id: str, token: str, *, lost: bool) -> SharedSessionDescriptor:
        """Release one failed member; restart/lose it without killing siblings."""
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            turn = member.current_turn
        if turn is not None:
            with contextlib.suppress(Exception):
                turn.interrupt()
            with contextlib.suppress(Exception):
                self.complete_turn(thread_id, token, turn)
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            transport = member.transport
            observer_cursor = member.observer_cursor
            # An admitted turn holds the member lock until whoever detaches it
            # releases it: ``complete_turn`` above, or, when that failed, this
            # teardown. ``threading.Lock`` has no owner, so ``locked()`` cannot
            # tell that from a resident start, a turn still in admission or a
            # fork of this source, each of which releases its own acquisition.
            # Releasing theirs would let a second launch or turn run beside it.
            release_turn_lock = member.current_turn is not None
            member.transport = None
            member.current_turn = None
            member.activation_processes = None
            member.observer_cursor = None
            member.tool_mark_reader = None
            member.lease_token = None
            member.lease_owner = None
            # Teardown owns the member now, so a stale acknowledgement must not
            # replay against whatever state the restart leaves behind.
            member.settlement_receipt = None
            member.context_status = "lost" if lost else member.context_status
            member.resident_state = (
                "lost" if lost else "restarting" if member.native_session_id else "stopped"
            )
            self._arm_idle_if_quiescent(group)
            descriptor = self._descriptor(group, member)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if observer_cursor is not None:
            with contextlib.suppress(Exception):
                self._manager.finish_guest_observation(group.spec, observer_cursor)
        if release_turn_lock:
            with contextlib.suppress(RuntimeError):
                member.turn_lock.release()
        return descriptor

    def residency_lost(
        self,
        thread_id: str,
        *,
        generation: int,
    ) -> SharedSessionDescriptor:
        """Close only the process whose renewable grant fence was rejected."""
        with self._lock:
            group, member = self._member(thread_id)
            if member.residency_generation != generation:
                return self._descriptor(group, member)
            turn = member.current_turn
            transport = member.transport
            # An active observation belongs to the turn, not to the resident
            # transport.  Keep it until complete_turn/abort_turn closes the
            # turn so the events already captured before residency loss are
            # still returned to the caller.  Drain only an orphaned cursor
            # when there is no turn left to own it.
            orphaned_observer_cursor = None
            orphaned_window = None
            orphaned_window_sampler = None
            if turn is None:
                orphaned_observer_cursor = member.observer_cursor
                member.observer_cursor = None
                member.tool_mark_reader = None
                # The window is the cursor's counterpart: a registration the
                # observer still holds. With no turn left to close it, this is
                # the only place that can, and leaving it open would keep
                # measuring a member that is no longer running a turn.
                orphaned_window = member.window
                orphaned_window_sampler = member.window_sampler
                member.window = None
                member.window_sampler = None
                member.window_checkpoints = None
            member.transport = None
            member.residency_generation = 0
            member.resident_state = "restarting" if member.native_session_id else "stopped"
        if turn is not None:
            with contextlib.suppress(Exception):
                turn.interrupt()
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if orphaned_window is not None and orphaned_window_sampler is not None:
            with contextlib.suppress(Exception):
                orphaned_window_sampler.close(orphaned_window)
        if orphaned_observer_cursor is not None:
            with contextlib.suppress(Exception):
                self._manager.finish_guest_observation(group.spec, orphaned_observer_cursor)
        with self._lock:
            return self._descriptor(group, member)

    def fork_native(
        self,
        source_thread_id: str,
        *,
        operation_id: str,
        source_native_session_id: str,
        source_native_turn_no: int,
        target_thread_id: str,
        target_slot_id: int,
        source_launch: ResidentLaunch,
        target_launch: ResidentLaunch,
    ) -> NativeSessionForkResult:
        """Fork immediately, then prove the child live before returning."""
        if self._native_forker is None:
            raise SandboxError("native session fork is not configured on this host")
        with self._lock:
            group, source = self._member(source_thread_id)
        self._recover_start_archive_completion(group)
        with self._lock:
            current_group, current_source = self._member(source_thread_id)
            if current_group is not group or current_source is not source:
                raise SessionLeaseLost("source membership changed before fork admission")
            self._require_mutation_admission(group)
            mutex = group.fork_mutex
            activity_token = f"fork:{operation_id}"
            if activity_token in group.operation_tokens:
                raise SessionBusy("native session fork is already running")
            group.operation_tokens[activity_token] = source_thread_id
            group.idle_deadline = None
        with mutex:
            try:
                if not source.turn_lock.acquire(blocking=False):
                    raise SessionBusy("source chat is busy")
                try:
                    return self._fork_native_locked_source(
                        group,
                        source,
                        operation_id=operation_id,
                        source_thread_id=source_thread_id,
                        source_native_session_id=source_native_session_id,
                        source_native_turn_no=source_native_turn_no,
                        target_thread_id=target_thread_id,
                        target_slot_id=target_slot_id,
                        source_launch=source_launch,
                        target_launch=target_launch,
                    )
                finally:
                    source.turn_lock.release()
            finally:
                with self._lock:
                    group.operation_tokens.pop(activity_token, None)
                    self._arm_idle_if_quiescent(group)

    def _fork_native_locked_source(
        self,
        group: _Group,
        source: _Member,
        *,
        operation_id: str,
        source_thread_id: str,
        source_native_session_id: str,
        source_native_turn_no: int,
        target_thread_id: str,
        target_slot_id: int,
        source_launch: ResidentLaunch,
        target_launch: ResidentLaunch,
    ) -> NativeSessionForkResult:
        """Run one fork while only the source member's turn lock is held."""
        if source_launch.plugins != target_launch.plugins:
            raise SandboxError("native fork cannot change plugins")
        # Docker start and CLI initialization can block for seconds. Neither
        # happens under the supervisor-wide metadata lock, so sibling turns
        # and terminals continue while this source is being prepared.
        self._ensure_running(group)
        stale_transport: ResidentTransport[DialectRuntime] | None = None
        with self._lock:
            self._require_mutation_admission(group)
            fork_lifecycle_generation = group.lifecycle_generation
            current_group, current_source = self._member(source_thread_id)
            if current_group is not group or current_source is not source:
                raise SessionLeaseLost("source membership changed before fork")
            if source.lease_token is not None or source.current_turn is not None:
                raise SessionBusy("source chat must be settled before native fork")
            if source.native_session_id != source_native_session_id:
                raise SessionLeaseLost("source native session changed before fork")
            if source_launch.native_session_id != source_native_session_id:
                raise SandboxError("source launch names a different native session")
            if source_launch.slot_id != source.resident_slot_id:
                raise SandboxError("source launch names a different resident slot")
            needs_restart = (
                source.transport is None
                or not source.transport.alive
                or source.residency_generation != source_launch.residency_generation
                or source.transport.launch.plugins != source_launch.plugins
            )
            if needs_restart:
                stale_transport = source.transport
                source.transport = None
                source.resident_state = "restarting"
            group.idle_deadline = None

        if stale_transport is not None:
            with contextlib.suppress(Exception):
                stale_transport.close()
        if needs_restart:
            self._ensure_transport(group, source, source_launch)

        with self._lock:
            if source.transport is None or not source.transport.alive:
                raise SandboxError("source native transport is not live")
            existing_target: _Member | None = None
            if target_thread_id in self._members:
                target_group, target = self._member(target_thread_id)
                if target_group is not group or target.native_session_id is None:
                    raise SandboxError("fork target identity is already occupied")
                if target.resident_slot_id != target_slot_id:
                    raise SandboxError("existing fork target owns a different resident slot")
                existing_target = target
            if not 0 <= target_slot_id < group.spec.resident_slot_capacity:
                raise SessionBusy("target resident slot is outside sandbox capacity")
            used_slots = {member.resident_slot_id for member in group.members.values()}
            if existing_target is None and target_slot_id in used_slots:
                raise SessionBusy("target resident slot is already occupied")
            source_transport = source.transport

        if existing_target is not None:
            child_launch = replace(
                target_launch,
                thread_id=target_thread_id,
                native_session_id=existing_target.native_session_id,
                slot_id=target_slot_id,
            )
            if not existing_target.turn_lock.acquire(blocking=False):
                raise SessionBusy("existing fork target is busy")
            try:
                self._ensure_transport(group, existing_target, child_launch)
            finally:
                existing_target.turn_lock.release()
            with self._lock:
                return NativeSessionForkResult(
                    operation_id=operation_id,
                    sandbox_id=group.spec.sandbox_id,
                    sandbox_generation=group.spec.generation,
                    container_id=group.inspection.container_id,
                    runtime_id=group.spec.runtime_id,
                    runtime_version=group.spec.runtime_version,
                    source_thread_id=source_thread_id,
                    source_native_session_id=source_native_session_id,
                    target_thread_id=target_thread_id,
                    target_native_session_id=str(existing_target.native_session_id),
                    target_resident_slot_id=existing_target.resident_slot_id,
                    source_process_generation=source.process_generation,
                    source_residency_generation=source.residency_generation,
                    target_process_generation=existing_target.process_generation,
                    target_residency_generation=existing_target.residency_generation,
                )

        target_native_id = self._native_forker(
            group.spec,
            source_transport,
            operation_id,
            replace(
                target_launch,
                thread_id=target_thread_id,
                native_session_id=None,
                slot_id=target_slot_id,
            ),
            source_native_turn_no,
        )
        # Some HTTP-backed CLIs persist the child successfully and then close
        # the server process that handled the fork request. A successful fork
        # receipt is not enough: the contract requires both source and child
        # to remain resident. Resume the exact source under the same residency
        # fence before exposing the child.
        if not source_transport.alive:
            with self._lock:
                current_group, current_source = self._member(source_thread_id)
                if current_group is not group or current_source is not source:
                    raise SessionLeaseLost("source membership changed after native fork")
                if source.transport is not source_transport:
                    raise SessionLeaseLost("source transport changed after native fork")
                source.transport = None
                source.resident_state = "restarting"
            with contextlib.suppress(Exception):
                source_transport.close()
            self._ensure_transport(group, source, source_launch)
        child_launch = replace(
            target_launch,
            thread_id=target_thread_id,
            native_session_id=target_native_id,
            slot_id=target_slot_id,
        )
        child = _Member(
            thread_id=target_thread_id,
            native_session_id=target_native_id,
            context_status="live",
            resident_state="starting",
            resident_slot_id=target_slot_id,
            residency_generation=target_launch.residency_generation,
        )
        try:
            child.transport = self._transport_factory.open(group.spec, child_launch)
            child.process_generation = 1
            child.resident_state = "live"
        except Exception:
            if child.transport is not None:
                with contextlib.suppress(Exception):
                    child.transport.close()
            raise

        with self._lock:
            if group.stopping or group.lifecycle_generation != fork_lifecycle_generation:
                child.transport.close()
                raise SessionLeaseLost("shared sandbox lifecycle changed during native fork")
            if target_thread_id in self._members:
                child.transport.close()
                raise SandboxError("fork target raced with another membership")
            group.members[target_thread_id] = child
            self._members[target_thread_id] = group
            return NativeSessionForkResult(
                operation_id=operation_id,
                sandbox_id=group.spec.sandbox_id,
                sandbox_generation=group.spec.generation,
                container_id=group.inspection.container_id,
                runtime_id=group.spec.runtime_id,
                runtime_version=group.spec.runtime_version,
                source_thread_id=source_thread_id,
                source_native_session_id=source_native_session_id,
                target_thread_id=target_thread_id,
                target_native_session_id=target_native_id,
                target_resident_slot_id=target_slot_id,
                source_process_generation=source.process_generation,
                source_residency_generation=source.residency_generation,
                target_process_generation=child.process_generation,
                target_residency_generation=child.residency_generation,
            )

    def operation_opened(self, thread_id: str) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one shared workspace operation without serializing it."""
        with self._lock:
            group, member = self._member(thread_id)
        self._recover_start_archive_completion(group)
        with self._lock:
            # This request performs its own fresh verification below.
            self._wait_for_start_locked(group, retry_failed=True)
            current_group, current_member = self._member(thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("shared membership changed before operation admission")
            self._require_mutation_admission(group)
            lifecycle_generation = group.lifecycle_generation
        self._ensure_running(group)
        with self._lock:
            self._wait_for_start_locked(group)
            current_group, current_member = self._member(thread_id)
            if (
                current_group is not group
                or current_member is not member
                or group.lifecycle_generation != lifecycle_generation
            ):
                raise SessionLeaseLost(
                    "shared sandbox lifecycle changed during operation admission"
                )
            self._require_mutation_admission(group)
            token = secrets.token_urlsafe(24)
            group.operation_tokens[token] = thread_id
            group.idle_deadline = None
            return token, self._descriptor(group, member)

    def prepare_environment(
        self,
        thread_id: str,
        environment: Any,
        preparer: Any,
        *,
        activation_owner: str | None = None,
    ) -> dict[str, Any]:
        """Serialize preparation with writers, preserving this activation's lease."""
        with self._lock:
            group, _ = self._member(thread_id)
        self._ensure_running(group)
        with self._lock:
            group, member = self._member(thread_id)
            backend = RetainedChatBackend(self._manager, group.spec)
            self._require_current_inspection(group)
            if preparer.status(backend, environment.binding_id).get("ready"):
                return preparer.status(backend, environment.binding_id)
            self._require_mutation_admission(group)
            if group.operation_tokens or any(
                m.current_turn is not None
                or (m.lease_token is not None and m.lease_token != activation_owner)
                for m in group.members.values()
            ):
                raise SessionBusy("Environment preparation waits for active workspace operations")
            cancelled = threading.Event()
            group.environment_cancel = cancelled
            group.environment_owner = activation_owner
            group.snapshotting = True
            generation = group.lifecycle_generation
            expected = group.inspection.container_id
            slots = [m.resident_slot_id for m in group.members.values()]
        monitor_stop = threading.Event()

        def monitor() -> None:
            while not monitor_stop.wait(0.5):
                with self._lock:
                    if group.lifecycle_generation != generation or (
                        activation_owner is not None and member.lease_token != activation_owner
                    ):
                        cancelled.set()
                        return

        monitor_thread = threading.Thread(
            target=monitor, daemon=True, name="environment-lease-fence"
        )
        monitor_thread.start()
        manifest = None
        try:
            manifest = self._manager.freeze_shared_writers(
                group.spec,
                slot_ids=slots,
                token=secrets.token_hex(16),
                expected_container_id=expected,
            )
            result = preparer.prepare(backend, environment, cancel=cancelled)
            self._require_current_inspection(group)
            if cancelled.is_set():
                raise SessionLeaseLost("Environment preparation lease was lost")
            return result
        finally:
            monitor_stop.set()
            monitor_thread.join(timeout=1)
            try:
                if manifest is not None:
                    # The only intentional new writers are registered environment services.
                    self._manager.thaw_shared_writers(
                        group.spec,
                        slot_ids=slots,
                        manifest=manifest,
                        require_exact_census=False,
                        expected_container_id=expected,
                    )
            finally:
                with self._lock:
                    group.snapshotting = False
                    group.environment_cancel = None
                    group.environment_owner = None
                    self._arm_idle_if_quiescent(group)
                    self._condition.notify_all()

    def cancel_environment(self, thread_id: str, activation_owner: str) -> None:
        """Only the preparing activation may revoke its long-running setup."""
        with self._lock:
            group, _ = self._member(thread_id)
            if group.environment_owner == activation_owner and group.environment_cancel:
                group.environment_cancel.set()

    def capture_snapshot(
        self,
        thread_id: str,
        *,
        capture: Callable[[RetainedChatBackend], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run one bounded Git capture while all existing writers are stopped.

        The metadata lock is held only while installing/removing the barrier.
        Docker exec and Git work happen outside it, so status/read paths stay
        responsive.  New turns, forks, terminals, and mutating sandbox manager
        operations observe ``snapshotting`` and wait/retry.  A normal idle
        stop is not a reason to abandon publication: start that exact retained
        container, capture it, and arm its ordinary idle deadline again.
        """
        with self._lock:
            group, _ = self._member(thread_id)
        self._recover_start_archive_completion(group)
        with self._lock:
            group, _ = self._member(thread_id)
            self._require_mutation_admission(group)
            if group.stopping:
                raise SessionBusy("shared workspace stop is in progress")
            if group.starting:
                raise SessionBusy("shared workspace start is in progress")
            if group.snapshotting:
                raise SessionBusy("shared workspace snapshot capture is already in progress")
            if not self._is_quiescent(group):
                raise SessionBusy("shared workspace still has active mutations")
            # The barrier goes up first, so Docker can be asked without the lock.
            group.snapshotting = True
            group.idle_deadline = None
            lifecycle_generation = group.lifecycle_generation
            slots = [member.resident_slot_id for member in group.members.values()]
        try:
            current = self._manager.inspect(group.spec.container_name)
            with self._lock:
                if current is None:
                    raise RetainedChatLost("shared sandbox container is missing")
                self._accept_inspection_locked(group, current)
                expected_container_id = group.inspection.container_id
        except BaseException:
            with self._lock:
                group.snapshotting = False
                self._arm_idle_if_quiescent(group)
                self._condition.notify_all()
            raise

        manifest: str | None = None
        capture_error: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            if not current.running:
                started = self._start_group(group)
                with self._lock:
                    if group.stopping or group.lifecycle_generation != lifecycle_generation:
                        raise SessionLeaseLost(
                            "shared sandbox lifecycle changed while starting snapshot capture"
                        )
                    group.inspection = started
            manifest = self._manager.freeze_shared_writers(
                group.spec,
                slot_ids=slots,
                token=secrets.token_hex(16),
                expected_container_id=expected_container_id,
            )
            result = capture(RetainedChatBackend(self._manager, group.spec))
        except BaseException as exc:
            capture_error = exc
        thaw_error: BaseException | None = None
        identity_error: BaseException | None = None
        if manifest is not None:
            try:
                self._require_current_inspection(group)
            except BaseException as exc:
                identity_error = exc
            try:
                self._manager.thaw_shared_writers(
                    group.spec,
                    slot_ids=slots,
                    manifest=manifest,
                    expected_container_id=expected_container_id,
                )
            except BaseException as exc:
                thaw_error = exc
            try:
                self._require_current_inspection(group)
            except BaseException as exc:
                if identity_error is None:
                    identity_error = exc
        with self._lock:
            group.snapshotting = False
            self._arm_idle_if_quiescent(group)
            self._condition.notify_all()
        if thaw_error is not None:
            raise thaw_error
        if identity_error is not None:
            raise identity_error
        if capture_error is not None:
            raise capture_error
        if result is None:
            raise SandboxError("shared snapshot capture returned no result")
        return result

    def _require_initial_archive_admission(
        self,
        group: _Group,
        member: _Member,
        *,
        expected_spec: ChatSandboxSpec | None,
        expected_container_id: str | None,
        own_barrier: bool = False,
    ) -> None:
        """Check, under the lock, everything the pre-prompt archive must exclude.

        ``own_barrier`` rechecks after this capture raised its snapshot barrier,
        which is then the one capture expected to be in progress.
        """
        if own_barrier:
            # This capture's barrier is the one snapshot expected; every other
            # writer and lifecycle fence is checked as usual.
            group.snapshotting = False
            try:
                self._require_mutation_admission(group)
            finally:
                group.snapshotting = True
        else:
            self._require_mutation_admission(group)
        if expected_spec is not None and group.spec != expected_spec:
            raise SandboxError("shared sandbox immutable identity/configuration changed")
        if expected_container_id is not None and group.inspection.container_id != (
            expected_container_id
        ):
            raise RetainedChatLost("shared sandbox container identity changed")
        if group.stopping:
            raise SessionBusy("shared workspace stop is in progress")
        if group.snapshotting and not own_barrier:
            raise SessionBusy("shared workspace snapshot capture is already in progress")
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        if member.current_turn is not None or member.transport is not None:
            raise SessionBusy("initial archive must precede resident process creation")
        if any(
            peer is not member and (peer.lease_token is not None or peer.current_turn is not None)
            for peer in group.members.values()
        ):
            raise SessionBusy("a sibling member is active during initial archive capture")

    def capture_initial_archive(
        self,
        thread_id: str,
        token: str,
        *,
        expected_spec: ChatSandboxSpec | None = None,
        expected_container_id: str | None = None,
        capture: Callable[
            [
                RetainedChatBackend,
                ChatSandboxSpec,
                tuple[SharedMemberManifest, ...],
            ],
            dict[str, Any],
        ],
    ) -> dict[str, Any]:
        """Capture the pre-prompt workspace owned by one fresh activation.

        Unlike a publication snapshot, this boundary deliberately permits the
        addressed activation lease: the runner has completed checkout/setup
        but has not created a resident CLI process or admitted its prompt.
        Every other writer remains excluded, and setup descendants are frozen
        while Docker copies the workspace.
        """
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            deadline = time.monotonic() + self.initial_archive_wait_s
            # A workspace read revalidates the bridge under a temporary start
            # fence, and the chat page reads the workspace while a new chat
            # prepares. Wait for that check as a prompt does, not fail on it.
            while group.starting or group.lifecycle_check is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionBusy(
                        "shared workspace start is in progress"
                        if group.starting
                        else "shared sandbox lifecycle check is in progress"
                    )
                self._condition.wait(timeout=min(remaining, 0.5))
            group, member = self._leased_member(thread_id, token)
            self._require_initial_archive_admission(
                group,
                member,
                expected_spec=expected_spec,
                expected_container_id=expected_container_id,
            )
            # The barrier goes up first: no new workspace request is admitted,
            # and the ones already running (reads, Git inspection, commands)
            # are given the same bounded time to finish.
            group.snapshotting = True
            group.idle_deadline = None
            try:
                while group.operation_tokens or group.terminal_tokens:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SessionBusy("shared workspace still has active mutations")
                    self._condition.wait(timeout=min(remaining, 0.5))
                # The wait released the lock; the lease and group may have moved.
                leased_group, leased_member = self._leased_member(thread_id, token)
                if leased_group is not group or leased_member is not member:
                    raise SessionLeaseLost("shared membership changed before initial archive")
                self._require_initial_archive_admission(
                    group,
                    member,
                    expected_spec=expected_spec,
                    expected_container_id=expected_container_id,
                    own_barrier=True,
                )
            except BaseException:
                group.snapshotting = False
                self._arm_idle_if_quiescent(group)
                self._condition.notify_all()
                raise
            observed = group.inspection
            lifecycle_generation = group.lifecycle_generation
            authoritative_spec = group.spec
            authoritative_members = self._archive_members_locked(group)
            expected_container_id = group.inspection.container_id
            activation_generation = member.activation_generation
            slots = [peer.resident_slot_id for peer in group.members.values()]
        try:
            current = self._manager.inspect(group.spec.container_name)
            if current is None:
                raise RetainedChatLost("shared sandbox container is missing")
            if not current.running:
                raise SandboxError("initial archive requires the prepared running sandbox")
            if current.container_id != expected_container_id:
                raise RetainedChatLost("shared sandbox container identity changed")
            with self._lock:
                if group.inspection == observed:
                    group.inspection = current
        except BaseException:
            with self._lock:
                group.snapshotting = False
                self._arm_idle_if_quiescent(group)
                self._condition.notify_all()
            raise

        manifest: str | None = None
        capture_error: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            manifest = self._manager.freeze_shared_writers(
                group.spec,
                slot_ids=slots,
                token=secrets.token_hex(16),
                expected_container_id=expected_container_id,
            )
            result = capture(
                RetainedChatBackend(self._manager, group.spec),
                authoritative_spec,
                authoritative_members,
            )
        except BaseException as exc:
            capture_error = exc
        preserve_frozen_writers = False
        release_snapshot_barrier = True
        marker_read_error: BaseException | None = None
        preservation_error: BaseException | None = None
        preservation_inspection_error: BaseException | None = None
        if capture_error is not None and manifest is not None:
            try:
                initial_marker = self._manager.read_initial_archive_marker(
                    authoritative_spec,
                    expected_container_id=expected_container_id,
                )
            except BaseException as exc:
                # A timed-out marker read cannot prove that the immutable
                # initial object was not prepared. Preserve the frozen bytes
                # exactly as if the marker had been observed.
                marker_read_error = exc
                preserve_frozen_writers = True
            else:
                preserve_frozen_writers = initial_marker is not None
            if preserve_frozen_writers:
                with self._lock:
                    group.initial_archive_preservation_pending = True
                try:
                    stopped = self.stop_if_idle(
                        thread_id,
                        generation=authoritative_spec.generation,
                        activation_generation=activation_generation,
                        reason="orphaned_attempt",
                    )
                except BaseException as exc:
                    preservation_error = exc
                    # A network fail-stop or shutdown can already own the same
                    # group. Do not sample its still-running pre-stop state and
                    # leave the snapshot fence orphaned after that exact stop
                    # completes. Every stop path clears ``stopping`` and
                    # notifies this condition; retain the frozen-writer barrier
                    # until the owning transition has a stable result to inspect.
                    with self._condition:
                        while group.stopping:
                            self._condition.wait(timeout=0.5)
                    try:
                        current = self._require_current_inspection(group)
                    except BaseException as inspection_exc:
                        preservation_inspection_error = inspection_exc
                        release_snapshot_barrier = False
                    else:
                        # A failed final upload leaves the exact container
                        # stopped with its bridge and pending marker retained.
                        # A pre-stop failure must keep this admission barrier:
                        # thawing or admitting a new writer would destroy the
                        # only bytes that can reconcile the initial object.
                        release_snapshot_barrier = not current.running
                else:
                    release_snapshot_barrier = not stopped.running
        thaw_error: BaseException | None = None
        identity_error: BaseException | None = None
        if manifest is not None and not preserve_frozen_writers:
            try:
                self._require_current_inspection(group)
            except BaseException as exc:
                identity_error = exc
            try:
                self._manager.thaw_shared_writers(
                    group.spec,
                    slot_ids=slots,
                    manifest=manifest,
                    expected_container_id=expected_container_id,
                )
            except BaseException as exc:
                thaw_error = exc
            try:
                self._require_current_inspection(group)
            except BaseException as exc:
                if identity_error is None:
                    identity_error = exc
        with self._lock:
            current_group = self._members.get(thread_id)
            current_member = (
                current_group.members.get(thread_id) if current_group is not None else None
            )
            lease_is_current = (
                current_group is group
                and current_member is member
                and not group.stopping
                and group.lifecycle_generation == lifecycle_generation
                and member.lease_token == token
            )
            # A concurrent exact stop can land after the inspection above but
            # before this bookkeeping lock. Honour that accepted STOPPED state
            # instead of recording a stale preservation barrier. Otherwise mark
            # this specific snapshot fence for release by a later exact stop.
            if (
                not release_snapshot_barrier
                and preserve_frozen_writers
                and not group.inspection.running
            ):
                release_snapshot_barrier = True
            if release_snapshot_barrier:
                group.initial_archive_preservation_pending = False
                group.snapshotting = False
                self._arm_idle_if_quiescent(group)
            elif preserve_frozen_writers:
                group.initial_archive_preservation_pending = True
            self._condition.notify_all()
        if preservation_error is not None:
            assert capture_error is not None
            _record_archive_failure_diagnostics(
                preservation_error,
                initial=capture_error,
                fail_stop_final=preservation_error,
            )
            preservation_error.add_note(
                "initial archive capture failed before fail-stop preservation: "
                f"{_archive_failure_diagnostic('initial', capture_error).render()}"
            )
            if marker_read_error is not None:
                preservation_error.add_note(
                    "the initial archive marker could not be read, so its presence "
                    "was treated as uncertain; error type: "
                    f"{_safe_error_type(marker_read_error)}"
                )
            if preservation_inspection_error is not None:
                preservation_error.add_note(
                    "the exact container state could not be confirmed after fail-stop; "
                    "the snapshot admission barrier remains set; error type: "
                    f"{_safe_error_type(preservation_inspection_error)}"
                )
            elif not release_snapshot_barrier:
                preservation_error.add_note(
                    "fail-stop did not stop the exact container; the snapshot "
                    "admission barrier remains set"
                )
            raise preservation_error from capture_error
        if thaw_error is not None:
            raise thaw_error
        if identity_error is not None:
            raise identity_error
        if capture_error is not None:
            _record_archive_failure_diagnostics(capture_error, initial=capture_error)
            if marker_read_error is not None:
                capture_error.add_note(
                    "the initial archive marker could not be read; the exact sandbox "
                    "was fail-stopped while its writers remained frozen"
                )
            raise capture_error
        if result is None:
            raise SandboxError("initial archive capture returned no result")
        if not lease_is_current:
            raise SessionLeaseLost("shared sandbox lifecycle changed during initial archive")
        return result

    def terminal_opened(self, thread_id: str) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one member-owned PTY against the shared group."""
        with self._lock:
            group, member = self._member(thread_id)
        self._recover_start_archive_completion(group)
        with self._lock:
            # This request performs its own fresh verification below.
            self._wait_for_start_locked(group, retry_failed=True)
            current_group, current_member = self._member(thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("shared membership changed before terminal admission")
            self._require_mutation_admission(group)
            lifecycle_generation = group.lifecycle_generation
        self._ensure_running(group)
        with self._lock:
            self._wait_for_start_locked(group)
            current_group, current_member = self._member(thread_id)
            if (
                current_group is not group
                or current_member is not member
                or group.lifecycle_generation != lifecycle_generation
            ):
                raise SessionLeaseLost("shared sandbox lifecycle changed during terminal admission")
            self._require_mutation_admission(group)
            token = secrets.token_urlsafe(24)
            group.terminal_tokens[token] = thread_id
            group.idle_deadline = None
            return token, self._descriptor(group, member)

    def terminal_closed(
        self,
        thread_id: str,
        token: str,
        *,
        last_activity_monotonic: float | None = None,
    ) -> SharedSessionDescriptor:
        """Release one PTY and refresh the group's single activity clock."""
        with self._lock:
            group, member = self._member(thread_id)
            if group.terminal_tokens.get(token) != thread_id:
                raise SessionLeaseLost("shared terminal lifecycle token is stale")
            now = self._clock()
            activity_at = now
            if last_activity_monotonic is not None:
                activity_at = float(last_activity_monotonic)
                if activity_at < 0 or not math.isfinite(activity_at):
                    raise ValueError("terminal last activity timestamp is invalid")
                activity_at = min(activity_at, now)
            # Validation comes first: a malformed close request is retryable
            # and must not consume the reference that keeps this group awake.
            group.terminal_tokens.pop(token, None)
            if self._is_quiescent(group) and group.inspection.running:
                group.idle_deadline = activity_at + self._idle_timeout_s
            return self._descriptor(group, member)

    def operation_closed(self, thread_id: str, token: str) -> SharedSessionDescriptor:
        """Release one workspace operation and refresh the group activity clock."""
        with self._lock:
            group, member = self._member(thread_id)
            if group.operation_tokens.get(token) != thread_id:
                raise SessionLeaseLost("shared workspace operation token is stale")
            group.operation_tokens.pop(token, None)
            self._arm_idle_if_quiescent(group)
            return self._descriptor(group, member)

    def backend(self, thread_id: str) -> RetainedChatBackend:
        """Return the one container backend shared by this member's group."""
        with self._lock:
            group, _ = self._member(thread_id)
            return RetainedChatBackend(self._manager, group.spec)

    def status(self, thread_id: str) -> SharedSessionDescriptor:
        """Return fresh Docker and member process state.

        Docker is asked outside the metadata lock, which every terminal request
        would otherwise hold across an inspect. An observation that a lifecycle
        call superseded while it was in flight never replaces that call's own.
        """
        stale_transport: ResidentTransport[DialectRuntime] | None = None
        with self._lock:
            group, member = self._member(thread_id)
            observed = group.inspection
            lifecycle_generation = group.lifecycle_generation
        current = self._manager.inspect(group.spec.container_name)
        with self._lock:
            current_group, current_member = self._member(thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("shared sandbox membership changed during observation")
            if group.inspection == observed and group.lifecycle_generation == lifecycle_generation:
                if current is None:
                    member.context_status = "lost"
                    member.resident_state = "lost"
                    raise RetainedChatLost("shared sandbox container is missing")
                self._accept_inspection_locked(group, current)
            elif group.container_lost:
                raise RetainedChatLost("shared sandbox container identity was lost")
            stale_transport = self._detach_dead_transport(member)
            descriptor = self._descriptor(group, member)
        if stale_transport is not None:
            with contextlib.suppress(Exception):
                stale_transport.close()
        return descriptor

    def list(self) -> builtins.list[SharedSessionDescriptor]:
        """Return one observation per known membership."""
        self.reconcile()
        stale_transports: builtins.list[ResidentTransport[DialectRuntime]] = []
        with self._lock:
            for group in self._groups.values():
                for member in group.members.values():
                    stale = self._detach_dead_transport(member)
                    if stale is not None:
                        stale_transports.append(stale)
            descriptors = [
                self._descriptor(group, member)
                for group in self._groups.values()
                for member in group.members.values()
            ]
        for transport in stale_transports:
            with contextlib.suppress(Exception):
                transport.close()
        return descriptors

    def list_for_thread(self, thread_id: str) -> builtins.list[SharedSessionDescriptor]:
        """Observe one known group without running host-wide reconciliation."""
        with self._lock:
            group, member = self._member(thread_id)
            spec = group.spec
            lifecycle_generation = group.lifecycle_generation
            inspection = group.inspection
            container_id = inspection.container_id

        current = self._manager.inspect(spec.container_name)
        stale_transports: builtins.list[ResidentTransport[DialectRuntime]] = []
        with self._lock:
            # A concurrent start can refresh inspection without advancing the
            # lifecycle generation. Never overwrite its accepted Docker state.
            if (
                self._groups.get(spec.sandbox_id) is not group
                or self._members.get(thread_id) is not group
                or group.members.get(thread_id) is not member
                or group.spec != spec
                or group.lifecycle_generation != lifecycle_generation
                or group.inspection != inspection
            ):
                raise SessionLeaseLost("shared sandbox lifecycle changed during observation")
            if current is None:
                self._mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            if current.container_id != container_id:
                self._mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container identity changed")
            if self._spec_from_inspection(current) != spec:
                raise SandboxError("shared sandbox immutable labels changed")
            self._accept_inspection_locked(group, current)
            for group_member in group.members.values():
                stale = self._detach_dead_transport(group_member)
                if stale is not None:
                    stale_transports.append(stale)
            descriptors = self._descriptors(group)
        for transport in stale_transports:
            with contextlib.suppress(Exception):
                transport.close()
        return descriptors

    def stop_if_idle(
        self,
        thread_id: str,
        *,
        generation: int,
        activation_generation: int | None = None,
        reason: Literal["idle", "orphaned_attempt", "lost", "unguarded"] = "idle",
        member_activation_generations: dict[str, int] | None = None,
        require_expired_deadline: bool = False,
        defer_archive: bool = False,
    ) -> SharedSessionDescriptor:
        """Close all member transports, then stop the group exactly once.

        ``stopping`` is an admission barrier across the blocking transport and
        Docker calls. Without it, a new activation can start the container in
        the gap after transports are detached but before the stale idle sweep
        issues ``docker stop``.

        Sweep callers also require an expired deadline under this barrier:
        activity can finish and refresh it after the sweep selects candidates.
        Direct stop requests retain their quiescence-only admission behavior.
        """
        with self._lock:
            group, requested = self._member(thread_id)
            if group.container_lost:
                raise RetainedChatLost("shared sandbox container identity was lost")
            if group.spec.generation != generation:
                raise SessionLeaseLost("shared sandbox generation is stale")
            if (
                activation_generation is not None
                and requested.activation_generation != activation_generation
            ):
                raise SessionLeaseLost("shared member activation generation is stale")
            if member_activation_generations is not None and any(
                member_activation_generations.get(member.thread_id) != member.activation_generation
                for member in group.members.values()
            ):
                raise SessionLeaseLost(
                    "shared sandbox member activation changed after stop request"
                )
            if group.stopping:
                raise SessionBusy("shared sandbox group stop is already in progress")
            if group.starting:
                raise SessionBusy("shared sandbox group start is in progress")
            if reason == "idle" and not self._is_quiescent(group):
                raise SessionBusy("shared sandbox group still has active members")
            if (
                reason == "idle"
                and require_expired_deadline
                and (group.idle_deadline is None or group.idle_deadline > self._clock())
            ):
                raise SessionBusy("shared sandbox group inactivity deadline has not elapsed")
            check = self._begin_lifecycle_check_locked(group)
        try:
            result = self._stop_checked_group(
                check, requested, reason=reason, defer_archive=defer_archive
            )
        finally:
            with self._lock:
                self._end_lifecycle_check_locked(check)
        with self._lock:
            return replace(result, group_busy=not self._is_quiescent(group))

    def _stop_checked_group(
        self,
        check: _LifecycleCheck,
        requested: _Member,
        *,
        reason: Literal["idle", "orphaned_attempt", "lost", "unguarded"],
        defer_archive: bool,
    ) -> SharedSessionDescriptor:
        """Inspect and classify a reserved group before its existing stop transition."""
        group = check.group
        current = self._manager.inspect(check.spec.container_name)
        with self._lock:
            self._require_lifecycle_check_locked(check)
            if current is None:
                self._mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            self._accept_inspection_locked(group, current)
            read_marker = (
                not current.running
                and self._stopped_archive_hook is not None
                and not group.stop_archive_blocked
                and not group.stop_archive_pending
            )
        marker_payload = None
        marker_prepared = False
        if read_marker:
            try:
                marker_payload = self._read_final_marker(check.spec, current)
                if marker_payload is None and current.status is not ChatSandboxStatus.CREATED:
                    marker_prepared = self._prepare_markerless_final(check.spec, current)
            except BaseException:
                with self._lock:
                    self._require_lifecycle_check_locked(check)
                    group.orphan_finalization_pending = True
                    group.orphan_marker_unreadable = True
                raise
        created_cleanup = False
        quarantine_unreadable = False
        with self._lock:
            self._require_lifecycle_check_locked(check)
            owned_bridge = self._manager.network_for(group.spec)
            blocked = group.stop_archive_blocked
            retry_pending = (
                group.stop_archive_pending
                or group.orphan_finalization_pending
                or group.start_archive_completion_pending
            )
            quarantine_unreadable = current.running and group.orphan_marker_unreadable
            if (
                not current.running
                and self._stopped_archive_hook is not None
                and not blocked
                and not group.stop_archive_pending
            ):
                group.orphan_marker_unreadable = False
                if marker_payload is not None:
                    retry_pending = True
                elif (
                    current.status is ChatSandboxStatus.CREATED
                    and not group.stop_archive_pending
                    and not group.start_archive_completion_pending
                ):
                    # Docker create attaches the owned bridge before the first
                    # start fence exists. A canceled pre-start activation has no
                    # native history to archive; clean up that exact never-run
                    # container without inventing a final marker.
                    created_cleanup = True
                    group.orphan_finalization_pending = False
                    retry_pending = False
                elif marker_prepared:
                    # The dynamic controller atomically wrote an explicit
                    # disabled-policy or skipped-final marker.  Finish that
                    # no-upload lifecycle through the normal retry path and
                    # release its bridge.
                    retry_pending = True
                else:
                    # No policy has been delivered (or a static archive service
                    # cannot record a skip). Unclassified native history stays
                    # fenced with its bridge; never fabricate a final archive.
                    blocked = True
                    group.stop_archive_blocked = True
                    group.stop_archive_pending = True
                    group.stop_archive_members = self._archive_members_locked(group)
                    retry_pending = True
            if retry_pending and not group.stop_archive_pending and not quarantine_unreadable:
                group.stop_archive_pending = self._stopped_archive_hook is not None
                group.stop_archive_members = (
                    self._archive_members_locked(group)
                    if self._stopped_archive_hook is not None
                    else None
                )
                retry_pending = group.stop_archive_pending
            if quarantine_unreadable or blocked:
                group.stopping = True
                group.idle_deadline = None
            elif not current.running and owned_bridge not in current.networks and not retry_pending:
                for member in group.members.values():
                    member.resident_state = "lost" if reason == "lost" else "stopped"
                    if reason == "lost":
                        member.context_status = "lost"
                group.idle_deadline = None
                return self._descriptor(group, requested)
            elif retry_pending and not created_cleanup:
                group.stopping = True
                group.idle_deadline = None
            else:
                group.stopping = True
                group.lifecycle_generation += 1
                group.idle_deadline = None
                group.operation_tokens.clear()
                group.terminal_tokens.clear()
                transports = [
                    member.transport
                    for member in group.members.values()
                    if member.transport is not None
                ]
                observations = [
                    (group.spec, member.observer_cursor)
                    for member in group.members.values()
                    if member.observer_cursor is not None
                ]
                interrupted_turn_locks: builtins.list[Any] = []
                for member in group.members.values():
                    if member.current_turn is not None and member.turn_lock.locked():
                        interrupted_turn_locks.append(member.turn_lock)
                    member.transport = None
                    member.current_turn = None
                    member.activation_processes = None
                    member.observer_cursor = None
                    member.tool_mark_reader = None
                    member.lease_token = None
                    member.lease_owner = None
                    member.resident_state = "lost" if reason == "lost" else "stopped"
                    if reason == "lost":
                        member.context_status = "lost"
                archive_members = self._archive_members_locked(group)
                expected_container_id = group.inspection.container_id

        if quarantine_unreadable:
            try:
                quarantined = self._manager.stop(
                    group.spec,
                    expected_container_id=current.container_id,
                    release_networks=False,
                )
                with self._lock:
                    self._accept_inspection_locked(group, quarantined)
                    for member in group.members.values():
                        member.resident_state = "stopped"
                    group.idle_deadline = None
            finally:
                with self._lock:
                    group.stopping = False
                    self._condition.notify_all()
            raise SandboxError("shared sandbox final archive marker remains unreadable")

        if blocked:
            try:
                self._stop_blocked_group(group, current)
            finally:
                with self._lock:
                    group.stopping = False
                    self._condition.notify_all()
            with self._lock:
                if group.stop_archive_blocked:
                    raise SandboxError("shared sandbox final archive skip could not be persisted")
                return self._descriptor(group, requested)

        if retry_pending:
            try:
                self._retry_stopped_archive(group, persist=not defer_archive)
            finally:
                with self._lock:
                    group.stopping = False
                    self._condition.notify_all()
            with self._lock:
                return self._descriptor(group, requested)

        inspection: ChatSandboxInspection | None = None
        flush_error: Exception | None = None
        try:
            # Native transports are closed before Docker stop so each runtime
            # can flush its own session history into the retained layer.
            for transport in transports:
                try:
                    transport.close()
                except Exception as exc:
                    if flush_error is None:
                        flush_error = exc
                    logger.exception(
                        "native transport close failed before sandbox final archive",
                        extra={
                            "event": "sandbox_native_history_flush_failed",
                            "sandbox_id": group.spec.sandbox_id,
                            "generation": group.spec.generation,
                        },
                    )
            for spec, cursor in observations:
                with contextlib.suppress(Exception):
                    self._manager.finish_guest_observation(spec, cursor)
            for turn_lock in interrupted_turn_locks:
                with contextlib.suppress(RuntimeError):
                    turn_lock.release()
            if flush_error is not None:
                # Native history may be incomplete, so this lifecycle is never
                # archived. Record the skip, exact-stop, and finish without
                # upload; the next resume relies on the ordinary native checks.
                with self._lock:
                    group.stop_archive_pending = self._stopped_archive_hook is not None
                    group.stop_archive_blocked = True
                    group.stop_archive_members = (
                        archive_members if self._stopped_archive_hook is not None else None
                    )
                inspection = self._stop_blocked_group(group, current)
                with self._lock:
                    skip_persisted = not group.stop_archive_blocked
                if not skip_persisted:
                    raise SandboxError(
                        "native flush failure archive skip could not be persisted"
                    ) from flush_error
            elif created_cleanup:
                inspection = self._manager.stop(
                    group.spec,
                    expected_container_id=expected_container_id,
                    release_networks=True,
                )
            else:
                if self._stopped_archive_hook is not None:
                    with self._lock:
                        group.stop_archive_pending = True
                        group.stop_archive_members = archive_members
                if self._stopped_archive_prepare_hook is not None:
                    try:
                        self._stopped_archive_prepare_hook(group.spec, current)
                    except Exception:
                        if not defer_archive:
                            raise
                        # Network proof is already lost. Archive preparation
                        # failure cannot leave this group running while later
                        # groups wait. Skip this lifecycle's final archive and
                        # finish the exact stop without upload.
                        with self._lock:
                            group.stop_archive_pending = True
                            group.stop_archive_blocked = True
                            group.stop_archive_members = archive_members
                        inspection = self._stop_blocked_group(group, current)
                        with self._lock:
                            skip_persisted = not group.stop_archive_blocked
                        if not skip_persisted:
                            raise
                if inspection is None:
                    inspection = self._manager.stop(
                        group.spec,
                        expected_container_id=expected_container_id,
                        release_networks=self._stopped_archive_hook is None,
                    )
                    # This is synchronous and fail-closed. A failed upload remains
                    # pinned to the same policy revision and leaves the stopped
                    # container/network as the durable retry marker.
                    if not defer_archive:
                        inspection = self._persist_stopped_archive(
                            group,
                            inspection,
                            archive_members,
                        )
        except BaseException:
            failed_inspection: ChatSandboxInspection | None = None
            with contextlib.suppress(Exception):
                failed_inspection = self._manager.inspect(group.spec.container_name)
            with self._lock:
                if inspection is not None:
                    self._accept_inspection_locked(group, inspection)
                    for member in group.members.values():
                        member.resident_state = "lost" if reason == "lost" else "stopped"
                        if reason == "lost":
                            member.context_status = "lost"
                    group.idle_deadline = None
                elif failed_inspection is None:
                    self._mark_container_lost_locked(group)
                else:
                    try:
                        self._accept_inspection_locked(group, failed_inspection)
                    except RetainedChatLost:
                        pass
                    else:
                        for member in group.members.values():
                            member.resident_state = (
                                "restarting" if member.native_session_id else "stopped"
                            )
                group.stopping = False
                if inspection is None:
                    self._arm_idle_if_quiescent(group, allow_lifecycle_check=True)
                self._condition.notify_all()
            raise
        assert inspection is not None
        with self._lock:
            self._accept_inspection_locked(group, inspection)
            for member in group.members.values():
                member.resident_state = "lost" if reason == "lost" else "stopped"
                if reason == "lost":
                    member.context_status = "lost"
            group.stopping = False
            group.idle_deadline = None
            descriptor = self._descriptor(group, requested)
            self._condition.notify_all()
            return descriptor

    def begin_archive(
        self,
        thread_id: str,
        *,
        request_id: str,
        generation: int,
        container_id: str,
    ) -> tuple[ChatSandboxSpec, dict[str, Any]]:
        """Hold a stopped group's admission barrier across S3 verification."""
        with self._lock:
            group, _ = self._member(thread_id)
            if group.spec.generation != generation:
                raise SessionLeaseLost("sandbox archive generation is stale")
            if group.archive_request_id not in (None, request_id):
                raise SessionBusy("another sandbox archive is in progress")
            self._require_deletion_lifecycle_admission(group)
            if group.archive_request_id is None:
                self._require_mutation_admission(group)
            if group.archive_request_id is None and not self._is_quiescent(group):
                raise SessionBusy("sandbox archive requires an idle group")
            check = self._begin_lifecycle_check_locked(group)
        try:
            current = self._manager.require_stopped_archive(check.spec, container_id=container_id)
            with self._lock:
                self._require_lifecycle_check_locked(check)
                self._accept_inspection_locked(group, current)
                if group.archive_request_id is None:
                    group.lifecycle_generation += 1
                group.archive_request_id = request_id
                group.stopping = True
                group.idle_deadline = None
                return group.spec, {
                    "spec": asdict(group.spec),
                    "inspection": asdict(current),
                    "members": [asdict(item) for item in self._descriptors(group)],
                }

        finally:
            with self._lock:
                self._end_lifecycle_check_locked(check)

    def abort_archive(self, spec: ChatSandboxSpec, *, request_id: str, container_id: str) -> None:
        """Release only the caller's archival barrier after a failed operation."""
        with self._lock:
            group = self._groups.get(spec.sandbox_id)
            if group is None:
                return
            if group.spec != spec or group.inspection.container_id != container_id:
                raise SessionLeaseLost("sandbox archive identity changed")
            if group.archive_request_id not in (None, request_id):
                raise SessionLeaseLost("sandbox archive request is stale")
            if group.lifecycle_check is not None:
                raise SessionBusy("shared sandbox lifecycle check is in progress")
            if group.archive_request_id == request_id:
                group.archive_request_id = None
                group.stopping = False
                group.lifecycle_generation += 1
                self._arm_idle_if_quiescent(group)
                self._condition.notify_all()

    def commit_archive(self, spec: ChatSandboxSpec, *, request_id: str, container_id: str) -> None:
        """Remove an exact stopped group after durable archive authorization.

        The caller persists its verified commit receipt before invoking this
        method. That receipt permits replay after a crash between Docker rm
        and the final acknowledgment. An absent original is complete only if
        its stable name is also absent; a replacement is always rejected.
        """
        with self._lock:
            group = self._groups.get(spec.sandbox_id)
            if group is not None:
                if group.spec != spec or group.inspection.container_id != container_id:
                    raise SessionLeaseLost("sandbox archive identity changed")
                if group.archive_request_id not in (None, request_id):
                    raise SessionLeaseLost("sandbox archive request is stale")
                if group.archive_request_id is None and not group.container_lost:
                    # Refusing a busy deletion must not invalidate an active
                    # native fork merely by advancing the lifecycle clock.
                    self._require_deletion_lifecycle_admission(group)
                    self._require_mutation_admission(group)
                    if not self._is_quiescent(group):
                        raise SessionBusy("sandbox deletion requires an idle group")
                check = self._begin_lifecycle_check_locked(group)
            else:
                if spec.sandbox_id in self._creating:
                    raise SessionBusy("shared workspace creation is in progress")
                self._creating.add(spec.sandbox_id)
                check = None
        try:
            current = self._manager.inspect(spec.container_name)
            with self._lock:
                if check is not None:
                    self._require_lifecycle_check_locked(check)
                if current is not None and group is not None:
                    self._require_deletion_lifecycle_admission(group)
                    if group.archive_request_id is None:
                        self._require_mutation_admission(group, allow_lifecycle_check=True)
                    if group.archive_request_id is None and not self._is_quiescent(
                        group, allow_lifecycle_check=True
                    ):
                        raise SessionBusy("sandbox deletion requires an idle group")
                    self._accept_inspection_locked(group, current)
                    group.archive_request_id = request_id
                    group.stopping = True
                    group.idle_deadline = None
            if current is not None:
                self._manager.remove_stopped_archived(spec, container_id=container_id)
            # Both identities must be absent: a renamed original or a replacement
            # at the stable name cannot satisfy a durable deletion receipt.
            original = self._manager.inspect(container_id)
            replacement = self._manager.inspect(spec.container_name)
            with self._lock:
                if check is not None:
                    self._require_lifecycle_check_locked(check)
                    if (
                        group is not None
                        and group.archive_request_id != request_id
                        and current is not None
                    ):
                        raise SessionLeaseLost("sandbox archive request is stale")
                if original is not None or replacement is not None:
                    raise RetainedChatLost("archived sandbox identity changed during deletion")
                if group is not None:
                    for member in group.members.values():
                        member.context_status = "lost"
                        member.resident_state = "lost"
                        self._members.pop(member.thread_id, None)
                    self._groups.pop(spec.sandbox_id, None)
                self._condition.notify_all()
        finally:
            with self._lock:
                if check is not None:
                    self._end_lifecycle_check_locked(check)
                else:
                    self._creating.discard(spec.sandbox_id)
                    self._condition.notify_all()

    def sweep(self) -> builtins.list[SharedSessionDescriptor]:
        """Stop groups whose single refreshed inactivity deadline elapsed."""
        now = self._clock()
        with self._lock:
            candidates = [
                (next(iter(group.members)), group.spec.generation)
                for group in self._groups.values()
                if group.inspection.running
                and group.idle_deadline is not None
                and group.idle_deadline <= now
                and self._is_quiescent(group)
            ]
        stopped: builtins.list[SharedSessionDescriptor] = []
        for thread_id, generation in candidates:
            with contextlib.suppress(SessionBusy, SessionLeaseLost):
                stopped.append(
                    self.stop_if_idle(
                        thread_id,
                        generation=generation,
                        require_expired_deadline=True,
                    )
                )
        return stopped

    def _ensure_transport(
        self,
        group: _Group,
        member: _Member,
        launch: ResidentLaunch,
        *,
        observer: StageRecorder | None = None,
        lease_token: str | None = None,
    ) -> str:
        """Open or reuse this member's transport, reporting which one it was.

        ``lease_token`` names the activation a leased caller opens it for; the
        process is installed only while that lease is still the member's. A
        fork opens its unleased source and target without one.
        """
        with self._lock:
            current_group, current_member = self._member(member.thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("member identity changed before process launch")
            if lease_token is not None and member.lease_token != lease_token:
                raise SessionLeaseLost("shared member activation lease is stale")
            self._require_mutation_admission(group)
            # The retained member owns the locator. An omitted field in a
            # later activation must not turn a restart into a fresh chat.
            if member.native_session_id:
                if (
                    launch.native_session_id
                    and launch.native_session_id != member.native_session_id
                ):
                    raise SandboxError("activation requested a different native session")
                launch = replace(launch, native_session_id=member.native_session_id)
            lifecycle_generation = group.lifecycle_generation
            current = member.transport
            same_residency = (
                current is not None
                and current.launch.residency_generation == launch.residency_generation
            )
            if (
                current is not None
                and current.alive
                and current.compatible_with(launch)
                and same_residency
            ):
                if launch.residency_generation:
                    member.residency_generation = launch.residency_generation
                member.resident_state = "live"
                return "reuse"
            if current is not None and current.alive and not launch.credential:
                # A probe from a new turn intentionally omits credentials so a
                # HOT hit never rotates the residency grant. Preserve the
                # existing process until the caller explicitly obtains a
                # replacement grant.
                raise SandboxError("resident process restart requires a renewable credential")
            member.transport = None
            member.resident_state = "restarting" if member.native_session_id else "starting"
        if current is not None:
            with contextlib.suppress(Exception):
                current.close()
        with substage(observer, "cli_spawn"):
            transport = self._transport_factory.open(group.spec, launch)
        observed = transport.native_session_id
        if launch.native_session_id and observed and observed != launch.native_session_id:
            with contextlib.suppress(Exception):
                transport.close()
            raise SandboxError("resident transport resumed a different native session")
        if launch.residency_generation <= 0:
            with contextlib.suppress(Exception):
                transport.close()
            raise SandboxError("a new resident process needs a residency generation fence")
        launch_invalid = False
        with self._lock:
            rechecked_group = self._members.get(member.thread_id)
            rechecked_member = (
                rechecked_group.members.get(member.thread_id)
                if rechecked_group is not None
                else None
            )
            launch_invalid = (
                rechecked_group is not group
                or rechecked_member is not member
                or group.stopping
                or group.lifecycle_generation != lifecycle_generation
                or (lease_token is not None and member.lease_token != lease_token)
            )
            if not launch_invalid:
                member.transport = transport
                member.process_generation += 1
                member.residency_generation = launch.residency_generation
                member.native_session_id = transport.native_session_id or member.native_session_id
                # HOT means this exact process accepted another turn. Any
                # newly opened process is a native resume (or the first NEW
                # process), even when acquire observed the predecessor alive
                # before a model/MCP/residency compatibility check replaced it.
                member.context_status = "restored" if launch.native_session_id else "new"
                member.resident_state = "live"
        if launch_invalid:
            with contextlib.suppress(Exception):
                transport.close()
            raise SessionLeaseLost("shared sandbox lifecycle changed during process launch")
        # A freshly opened process is a native resume when there was a session
        # to resume, and a new CLI otherwise. It is never a reuse: the HOT
        # process returned above.
        return "resume" if launch.native_session_id else "new"

    def _ensure_running(self, group: _Group) -> None:
        current = self._manager.inspect(group.spec.container_name)
        if current is None:
            raise RetainedChatLost("shared sandbox container is missing")
        if group.stop_archive_pending:
            raise SessionBusy("shared workspace final archive retry is pending")
        if group.orphan_finalization_pending:
            raise SessionBusy("shared workspace orphan finalization is pending")
        group.inspection = self._start_group(group)

    def _recover_start_archive_completion(self, group: _Group) -> None:
        """Retry an idempotent post-start marker completion before admission."""
        with self._lock:
            pending = group.start_archive_completion_pending
        if pending:
            self._start_group(group)

    def _arm_idle_if_quiescent(self, group: _Group, *, allow_lifecycle_check: bool = False) -> None:
        if (
            self._is_quiescent(group, allow_lifecycle_check=allow_lifecycle_check)
            and group.inspection.running
            and not group.stop_archive_pending
            and not group.stop_archive_blocked
            and not group.orphan_finalization_pending
            and not group.start_archive_completion_pending
        ):
            group.idle_deadline = self._clock() + self._idle_timeout_s
        else:
            group.idle_deadline = None

    @staticmethod
    def _detach_dead_transport(member: _Member) -> ResidentTransport[DialectRuntime] | None:
        # The turn-lock owner controls transport replacement. Native fork
        # deliberately closes the source while copying its private state,
        # then resumes it under the same residency fence. A concurrent
        # heartbeat/status read must not detach that owned transport. Real
        # residency loss and forced stop use their own explicit invalidation
        # paths and still take effect while this lock is held.
        if member.turn_lock.locked():
            return None
        transport = member.transport
        if transport is None or transport.alive:
            return None
        member.transport = None
        member.resident_state = "restarting" if member.native_session_id else "stopped"
        return transport

    @staticmethod
    def _is_quiescent(group: _Group, *, allow_lifecycle_check: bool = False) -> bool:
        return (
            (allow_lifecycle_check or group.lifecycle_check is None)
            and not group.snapshotting
            and not group.starting
            and not group.stopping
            and not group.operation_tokens
            and not group.terminal_tokens
            and all(
                member.lease_token is None and member.current_turn is None
                for member in group.members.values()
            )
        )

    @staticmethod
    def _require_deletion_lifecycle_admission(group: _Group) -> None:
        """Preserve native flush and lifecycle archive fences during deletion."""
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        if (
            group.snapshotting
            or group.starting
            or group.initial_archive_preservation_pending
            or group.stop_archive_pending
            or group.stop_archive_blocked
            or group.orphan_finalization_pending
            or group.start_archive_completion_pending
        ):
            raise SessionBusy("sandbox lifecycle archive must finish before deletion")

    @staticmethod
    def _require_mutation_admission(group: _Group, *, allow_lifecycle_check: bool = False) -> None:
        if group.lifecycle_check is not None and not allow_lifecycle_check and not group.stopping:
            raise SessionBusy("shared sandbox lifecycle check is in progress")
        if group.snapshotting:
            raise SessionBusy("shared workspace snapshot capture is in progress")
        if group.stopping:
            raise SessionBusy("shared workspace stop is in progress")
        if group.starting:
            raise SessionBusy("shared workspace start is in progress")
        if group.stop_archive_blocked:
            raise SessionBusy("shared workspace final archive is blocked by native flush failure")
        if group.stop_archive_pending:
            raise SessionBusy("shared workspace final archive retry is pending")
        if group.orphan_finalization_pending:
            raise SessionBusy("shared workspace orphan finalization is pending")
        if group.start_archive_completion_pending:
            raise SessionBusy("shared workspace start archive completion is pending")

    def _member(self, thread_id: str) -> tuple[_Group, _Member]:
        group = self._members.get(thread_id)
        if group is None:
            raise SandboxError(f"chat {thread_id!r} has no shared sandbox membership")
        return group, group.members[thread_id]

    def _leased_member(self, thread_id: str, token: str) -> tuple[_Group, _Member]:
        group, member = self._member(thread_id)
        if not token or member.lease_token != token:
            raise SessionLeaseLost("shared member activation lease is stale")
        return group, member

    @staticmethod
    def _lease(
        group: _Group,
        member: _Member,
        *,
        facts: SandboxActionFacts | None = None,
        recorder: StageRecorder | None = None,
        call_id: str | None = None,
        network_verified: bool = False,
    ) -> SharedSessionLease:
        """Build the caller's receipt, including what this call did to the container."""
        assert member.lease_token is not None
        return SharedSessionLease(
            thread_id=member.thread_id,
            origin_thread_id=group.spec.thread_id,
            sandbox_id=group.spec.sandbox_id,
            container_id=group.inspection.container_id,
            container_name=group.inspection.name,
            token=member.lease_token,
            activation_generation=member.activation_generation,
            process_generation=member.process_generation,
            context_status=member.context_status,
            resident_state=member.resident_state,
            resident_slot_id=member.resident_slot_id,
            container_action=facts.container_action if facts is not None else "unknown",
            stages=list(recorder.stages) if recorder is not None else [],
            profiling_call_id=call_id,
            profiling_recorded=recorder is not None,
            profiling_truncated=recorder is not None and recorder.stopped_early(),
            network_verified=network_verified,
        )

    def _descriptors(self, group: _Group) -> builtins.list[SharedSessionDescriptor]:
        return [self._descriptor(group, member) for member in group.members.values()]

    @staticmethod
    def _descriptor(
        group: _Group,
        member: _Member,
        *,
        resident_action: str = "unknown",
        stages: builtins.list[dict[str, Any]] | None = None,
        call_id: str | None = None,
        recorded: bool = False,
        truncated: bool = False,
    ) -> SharedSessionDescriptor:
        return SharedSessionDescriptor(
            thread_id=member.thread_id,
            origin_thread_id=group.spec.thread_id,
            sandbox_id=group.spec.sandbox_id,
            generation=group.spec.generation,
            container_id=group.inspection.container_id,
            container_name=group.inspection.name,
            docker_status=group.inspection.status.value,
            running=group.inspection.running,
            group_busy=not SharedChatSessionSupervisor._is_quiescent(group),
            busy=member.lease_token is not None or member.current_turn is not None,
            terminal_count=sum(
                1 for owner in group.terminal_tokens.values() if owner == member.thread_id
            ),
            operation_count=sum(
                1 for owner in group.operation_tokens.values() if owner == member.thread_id
            ),
            snapshotting=group.snapshotting,
            activation_generation=member.activation_generation,
            process_generation=member.process_generation,
            residency_generation=member.residency_generation,
            resident_state=member.resident_state,
            resident_slot_id=member.resident_slot_id,
            last_settled_job_id=member.last_settled_job_id,
            native_session_id=member.native_session_id,
            idle_deadline_monotonic=group.idle_deadline,
            context_status=member.context_status,
            resident_action=resident_action,
            stages=list(stages) if stages else [],
            profiling_call_id=call_id,
            profiling_recorded=recorded,
            profiling_truncated=truncated,
        )

    @staticmethod
    def _spec_from_inspection(inspection: ChatSandboxInspection) -> ChatSandboxSpec:
        labels = inspection.labels
        try:
            if labels.get(CHAT_SHARED_LABEL) != "true":
                raise ValueError("not a shared-live sandbox")
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
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxError(
                f"shared container {inspection.name!r} has incomplete identity labels"
            ) from exc

    def guard_network(self) -> builtins.list[SharedSessionDescriptor]:
        """Stop every running group when the host can no longer show its rules.

        Every pass reads the host's tables back and reinstalls what it finds
        missing, so this only acts when that repair fails twice: a running
        group would otherwise go on without the boundary it was started
        behind, and anything a repository left running in it with it. Fail
        closed — stop them all, which releases their bridges and
        keeps their workspaces. Waking one reinstalls and verifies the rules
        before it runs again, and refuses while they cannot be.
        """
        with self._lock:
            running = [
                (next(iter(group.members)), group.spec.generation)
                for group in self._groups.values()
                if group.inspection.running and not group.stopping and group.members
            ]
        if not running:
            self._renew_idle_host_rules()
            return []
        try:
            self._manager.verify_host_rules()
            return []
        except SandboxError:
            pass
        try:
            # A failed check is never cached, so this is a full second attempt:
            # one Docker hiccup is not reason enough to stop every group.
            self._manager.verify_host_rules()
            return []
        except SandboxError as exc:
            logger.error(
                "shared_chat_network_unguarded",
                extra={
                    "event": "shared_chat_network_unguarded",
                    "groups": len(running),
                    "detail": str(exc)[:300],
                },
            )
        staged: builtins.list[tuple[str, int]] = []
        for thread_id, generation in running:
            try:
                self.stop_if_idle(
                    thread_id,
                    generation=generation,
                    reason="unguarded",
                    defer_archive=True,
                )
            except Exception:
                logger.exception(
                    "unguarded shared sandbox could not be stopped",
                    extra={
                        "event": "shared_chat_network_fail_stop_failed",
                        "thread_id": thread_id,
                        "generation": generation,
                    },
                )
            else:
                staged.append((thread_id, generation))

        # Compression and S3 upload can take minutes. No group remains running
        # on a host whose packet boundary is unproven while an earlier group's
        # final archive is in flight: every exact Docker stop above completes
        # before this second persistence/release phase begins.
        stopped: builtins.list[SharedSessionDescriptor] = []
        for thread_id, generation in staged:
            try:
                stopped.append(
                    self.stop_if_idle(
                        thread_id,
                        generation=generation,
                        reason="unguarded",
                    )
                )
            except Exception as exc:
                logger.error(
                    "unguarded shared sandbox final archive did not complete",
                    extra={
                        "event": "shared_chat_network_final_archive_failed",
                        "thread_id": thread_id,
                        "generation": generation,
                        "detail": _archive_failure_diagnostic("fail_stop_final", exc).render(),
                    },
                )
        return stopped

    def _renew_idle_host_rules(self) -> None:
        """Keep the host-rule verification fresh while no group runs.

        Nothing runs behind the rules, so nothing is read every pass and
        nothing is stopped. But the next start or wake must install and read
        them back first if their verification has lapsed, and on a quiet host
        it always has. Renewing it before it lapses keeps that off the start.
        A failure is logged once until a renewal succeeds again; the next
        activation repeats the same check and refuses on its own.
        """
        try:
            self._manager.renew_host_rules()
        except SandboxError as exc:
            if not self._idle_host_rules_failing:
                logger.warning(
                    "shared_chat_idle_host_rules_unavailable",
                    extra={
                        "event": "shared_chat_idle_host_rules_unavailable",
                        "detail": str(exc)[:300],
                    },
                )
            self._idle_host_rules_failing = True
        else:
            self._idle_host_rules_failing = False

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self._sweep_interval_s):
            with contextlib.suppress(Exception):
                self.guard_network()
            with contextlib.suppress(Exception):
                self.sweep()


__all__ = [
    "NativeSessionForkResult",
    "SharedChatSessionSupervisor",
    "SharedMemberManifest",
    "SharedSessionDescriptor",
    "SharedSessionLease",
    "SharedTurnBoundaryError",
    "SharedTurnTelemetry",
    "archive_failure_http_detail",
]
