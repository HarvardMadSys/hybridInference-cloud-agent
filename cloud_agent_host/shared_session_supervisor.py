"""Concurrent member supervisor for one shared retained sandbox workspace."""

from __future__ import annotations

import contextlib
import logging
import math
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.chat_sandbox import (
    CHAT_CPU_LIMIT_LABEL,
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
    RetainedChatBackend,
    RetainedChatLost,
)
from cloud_agent_host.drivers import DialectRuntime
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

if TYPE_CHECKING:
    import builtins

    from cloud_agent_host.tool_windows import ToolMarkReader

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
    tool_marks: tuple[tuple[float, str, str], ...] = ()
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
    stopping: bool = False
    lifecycle_generation: int = 0
    idle_deadline: float | None = None
    fork_mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)


NativeForker = Callable[
    [ChatSandboxSpec, ResidentTransport[DialectRuntime], str, ResidentLaunch, int],
    str,
]


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
        self._clock = clock
        self._transport_factory = transport_factory or ResidentTransportFactory(manager)
        self._native_forker = native_forker or self._transport_factory.fork_native
        self._groups: dict[str, _Group] = {}
        self._members: dict[str, _Group] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def idle_timeout_s(self) -> float:
        """Return the one group inactivity timeout."""
        return self._idle_timeout_s

    def start(self) -> None:
        """Reconstruct shared containers and start the group idle sweep."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.reconcile()
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

    def close(self) -> None:
        """Stop the supervisor process; leave Docker-owned state intact."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._sweep_interval_s + 1)
        self._thread = None
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

    def reconcile(self) -> builtins.list[SharedSessionDescriptor]:
        """Discover only containers labelled for the shared-live protocol."""
        found: builtins.list[SharedSessionDescriptor] = []
        specs: builtins.list[ChatSandboxSpec] = []
        for inspection in self._manager.list():
            if inspection.labels.get(CHAT_SHARED_LABEL) != "true":
                continue
            spec = self._spec_from_inspection(inspection)
            specs.append(spec)
            with self._lock:
                group = self._groups.get(spec.sandbox_id)
                if group is None:
                    # A replacement broker cannot reattach the old docker-exec
                    # pipes. Stop only a newly discovered orphan before
                    # adopting it; list() also calls reconcile(), so stopping a
                    # group already managed by this process would race every
                    # active turn with ordinary status polling.
                    inspection = self._manager.stop(spec)
                    group = _Group(spec=spec, inspection=inspection)
                    origin = _Member(thread_id=spec.thread_id, resident_slot_id=0)
                    group.members[origin.thread_id] = origin
                    self._groups[spec.sandbox_id] = group
                    self._members[origin.thread_id] = group
                else:
                    if group.spec != spec:
                        raise SandboxError("shared sandbox immutable labels changed")
                    if not inspection.running and inspection.networks and self._is_quiescent(group):
                        # Retry an idempotent release after an external stop or
                        # a prior stop whose Docker network cleanup failed.
                        # Active member/terminal operations fence this path
                        # from a concurrent wake or phase transition.
                        inspection = self._manager.stop(spec)
                    group.inspection = inspection
                found.extend(self._descriptors(group))
        self._manager.prune_orphan_networks(specs)
        return found

    def register_manifest(
        self,
        spec: ChatSandboxSpec,
        members: builtins.list[SharedMemberManifest],
        *,
        expected_container_id: str | None = None,
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
            group = self._groups.get(spec.sandbox_id)
            if group is None:
                inspection = (
                    self._manager.require_existing(spec, container_id=expected_container_id)
                    if expected_container_id
                    else self._manager.create(spec)
                )
                group = _Group(spec=spec, inspection=inspection)
                self._groups[spec.sandbox_id] = group
            elif group.spec != spec:
                raise SandboxError("shared sandbox immutable identity/configuration changed")
            if expected_container_id and group.inspection.container_id != expected_container_id:
                raise RetainedChatLost("shared sandbox container identity changed")

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
                    # A durable observation that the previous broker owned a
                    # live process is not an attachable local transport. Docker
                    # preserves the native session files, but the replacement
                    # broker has no stdin/stdout pipes and must resume it before
                    # claiming a HOT hit.
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
    ) -> SharedSessionLease:
        """Start one group if needed and lease only the addressed member."""
        self.register_manifest(spec, manifest, expected_container_id=expected_container_id)
        with self._lock:
            group, member = self._member(thread_id)
            transition_deadline = time.monotonic() + 30.0
            while group.snapshotting or group.stopping:
                remaining = transition_deadline - time.monotonic()
                if remaining <= 0:
                    transition = "snapshot" if group.snapshotting else "stop"
                    raise SessionBusy(f"shared workspace {transition} is in progress")
                self._condition.wait(timeout=min(remaining, 0.5))
            if member.lease_token is not None:
                if member.lease_owner == owner:
                    return self._lease(group, member)
                raise SessionBusy(f"chat {thread_id!r} already has an active turn")
            if member.current_turn is not None:
                raise SessionBusy(f"chat {thread_id!r} already has a running turn")
            # Fence stale attempt owners before doing anything observable to
            # the shared container. In particular, an old lease must not wake
            # a stopped workspace only to be rejected after ``docker start``.
            if activation_generation < member.activation_generation:
                raise SessionLeaseLost("member activation generation is stale")
            current = self._manager.inspect(group.spec.container_name)
            if current is None:
                member.context_status = "lost"
                member.resident_state = "lost"
                raise RetainedChatLost("shared sandbox container is missing")
            if expected_container_id and current.container_id != expected_container_id:
                member.context_status = "lost"
                member.resident_state = "lost"
                raise RetainedChatLost("shared sandbox container identity changed")
            group.inspection = self._manager.start(
                group.spec,
                network_phase="agent" if has_settled_context else "setup",
            )
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

        with self._lock:
            return self._lease(group, member)

    def ensure_resident(
        self,
        thread_id: str,
        token: str,
        launch: ResidentLaunch,
    ) -> SharedSessionDescriptor:
        """Open or reuse the member transport after workspace preparation."""
        with self._lock:
            group, member = self._leased_member(thread_id, token)
            self._require_mutation_admission(group)
        # CLI initialization may block. The member lock, not the group lock,
        # leaves siblings able to initialize and execute simultaneously.
        with member.turn_lock:
            self._ensure_transport(group, member, launch)
        with self._lock:
            return self._descriptor(group, member)

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
                        "workspace broker has no guest observer client",
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
            with self._lock:
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
        return turn

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
                    "workspace broker has no guest observer client",
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
                            "workspace broker returned invalid process telemetry",
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
        with self._lock:
            _, member = self._leased_member(thread_id, token)
            if member.current_turn is not turn:
                raise SessionLeaseLost("member turn generation is stale")
            member.current_turn = None
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
            return reduce_window(
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
        is how the broker builds it -- produced no checkpoints at all, so a
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

    def abort_turn(
        self,
        thread_id: str,
        token: str,
        turn: ResidentTurn[DialectRuntime],
    ) -> SharedTurnTelemetry:
        """Fence a turn whose broker stream disappeared before its result.

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
            # ``complete_turn`` may have cleared ``current_turn`` and then
            # failed before releasing the lock. Abandon owns this member's
            # teardown, so the lock state itself is the authoritative cleanup
            # signal here.
            release_turn_lock = member.turn_lock.locked()
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
            member.resident_state = "lost" if lost else "restarting"
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
            self._require_mutation_admission(group)
            self._ensure_running(group)
            token = secrets.token_urlsafe(24)
            group.operation_tokens[token] = thread_id
            group.idle_deadline = None
            return token, self._descriptor(group, member)

    def capture_snapshot(
        self,
        thread_id: str,
        *,
        capture: Callable[[RetainedChatBackend], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run one bounded Git capture while all existing writers are stopped.

        The metadata lock is held only while installing/removing the barrier.
        Docker exec and Git work happen outside it, so status/read paths stay
        responsive.  New turns, forks, terminals, and mutating broker
        operations observe ``snapshotting`` and wait/retry.  A normal idle
        stop is not a reason to abandon publication: start that exact retained
        container, capture it, and arm its ordinary idle deadline again.
        """
        with self._lock:
            group, _ = self._member(thread_id)
            if group.stopping:
                raise SessionBusy("shared workspace stop is in progress")
            if group.snapshotting:
                raise SessionBusy("shared workspace snapshot capture is already in progress")
            if not self._is_quiescent(group):
                raise SessionBusy("shared workspace still has active mutations")
            current = self._manager.inspect(group.spec.container_name)
            if current is None:
                raise RetainedChatLost("shared sandbox container is missing")
            group.inspection = current
            group.snapshotting = True
            group.idle_deadline = None
            lifecycle_generation = group.lifecycle_generation
            slots = [member.resident_slot_id for member in group.members.values()]

        manifest: str | None = None
        capture_error: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            if not current.running:
                started = self._manager.start(group.spec, network_phase="agent")
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
            )
            result = capture(RetainedChatBackend(self._manager, group.spec))
        except BaseException as exc:
            capture_error = exc
        thaw_error: BaseException | None = None
        if manifest is not None:
            try:
                self._manager.thaw_shared_writers(
                    group.spec,
                    slot_ids=slots,
                    manifest=manifest,
                )
            except BaseException as exc:
                thaw_error = exc
        with self._lock:
            group.snapshotting = False
            self._arm_idle_if_quiescent(group)
            self._condition.notify_all()
        if thaw_error is not None:
            raise thaw_error
        if capture_error is not None:
            raise capture_error
        if result is None:
            raise SandboxError("shared snapshot capture returned no result")
        return result

    def terminal_opened(self, thread_id: str) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one member-owned PTY against the shared group."""
        with self._lock:
            group, member = self._member(thread_id)
            self._require_mutation_admission(group)
            self._ensure_running(group)
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
        """Return fresh Docker and member process state."""
        stale_transport: ResidentTransport[DialectRuntime] | None = None
        with self._lock:
            group, member = self._member(thread_id)
            current = self._manager.inspect(group.spec.container_name)
            if current is None:
                member.context_status = "lost"
                member.resident_state = "lost"
                raise RetainedChatLost("shared sandbox container is missing")
            group.inspection = current
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

    def stop_if_idle(
        self,
        thread_id: str,
        *,
        generation: int,
        reason: Literal["idle", "orphaned_attempt", "lost"] = "idle",
    ) -> SharedSessionDescriptor:
        """Close all member transports, then stop the group exactly once.

        ``stopping`` is an admission barrier across the blocking transport and
        Docker calls. Without it, a new activation can start the container in
        the gap after transports are detached but before the stale idle sweep
        issues ``docker stop``.
        """
        with self._lock:
            group, requested = self._member(thread_id)
            if group.spec.generation != generation:
                raise SessionLeaseLost("shared sandbox generation is stale")
            if group.stopping:
                raise SessionBusy("shared sandbox group stop is already in progress")
            if reason == "idle" and not self._is_quiescent(group):
                raise SessionBusy("shared sandbox group still has active members")
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
        try:
            for transport in transports:
                with contextlib.suppress(Exception):
                    transport.close()
            for spec, cursor in observations:
                with contextlib.suppress(Exception):
                    self._manager.finish_guest_observation(spec, cursor)
            for turn_lock in interrupted_turn_locks:
                with contextlib.suppress(RuntimeError):
                    turn_lock.release()
            current = self._manager.inspect(group.spec.container_name)
            if current is None:
                raise RetainedChatLost("shared sandbox container is missing")
            # ``ChatSandboxManager.stop`` is deliberately idempotent: when
            # Docker already stopped the container it still releases the
            # retained phase network. Always delegate so an external stop, or
            # a retry after network cleanup failed, cannot leak the chat's
            # private subnet.
            inspection = self._manager.stop(group.spec)
        except BaseException:
            failed_inspection: ChatSandboxInspection | None = None
            with contextlib.suppress(Exception):
                failed_inspection = self._manager.inspect(group.spec.container_name)
            with self._lock:
                if failed_inspection is None:
                    for member in group.members.values():
                        member.context_status = "lost"
                        member.resident_state = "lost"
                else:
                    group.inspection = failed_inspection
                    for member in group.members.values():
                        member.resident_state = (
                            "restarting" if member.native_session_id else "stopped"
                        )
                group.stopping = False
                self._arm_idle_if_quiescent(group)
                self._condition.notify_all()
            raise
        with self._lock:
            group.inspection = inspection
            for member in group.members.values():
                member.resident_state = "lost" if reason == "lost" else "stopped"
                if reason == "lost":
                    member.context_status = "lost"
            group.stopping = False
            group.idle_deadline = None
            descriptor = self._descriptor(group, requested)
            self._condition.notify_all()
            return descriptor

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
                    )
                )
        return stopped

    def _ensure_transport(
        self,
        group: _Group,
        member: _Member,
        launch: ResidentLaunch,
    ) -> None:
        with self._lock:
            current_group, current_member = self._member(member.thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("member identity changed before process launch")
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
                return
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

    def _ensure_running(self, group: _Group) -> None:
        current = self._manager.inspect(group.spec.container_name)
        if current is None:
            raise RetainedChatLost("shared sandbox container is missing")
        network_phase: Literal["setup", "agent"] = (
            "agent"
            if any(member.native_session_id for member in group.members.values())
            else "setup"
        )
        group.inspection = self._manager.start(
            group.spec,
            network_phase=network_phase,
        )

    def _arm_idle_if_quiescent(self, group: _Group) -> None:
        if self._is_quiescent(group) and group.inspection.running:
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
    def _is_quiescent(group: _Group) -> bool:
        return (
            not group.snapshotting
            and not group.stopping
            and not group.operation_tokens
            and not group.terminal_tokens
            and all(
                member.lease_token is None and member.current_turn is None
                for member in group.members.values()
            )
        )

    @staticmethod
    def _require_mutation_admission(group: _Group) -> None:
        if group.snapshotting:
            raise SessionBusy("shared workspace snapshot capture is in progress")
        if group.stopping:
            raise SessionBusy("shared workspace stop is in progress")

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
    def _lease(group: _Group, member: _Member) -> SharedSessionLease:
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
        )

    def _descriptors(self, group: _Group) -> builtins.list[SharedSessionDescriptor]:
        return [self._descriptor(group, member) for member in group.members.values()]

    @staticmethod
    def _descriptor(group: _Group, member: _Member) -> SharedSessionDescriptor:
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
                stop_timeout_s=int(labels[CHAT_STOP_TIMEOUT_LABEL]),
                shared_live=True,
                resident_slot_capacity=int(labels[CHAT_RESIDENT_CAPACITY_LABEL]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxError(
                f"shared container {inspection.name!r} has incomplete identity labels"
            ) from exc

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self._sweep_interval_s):
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
]
