"""In-memory ownership the supervisor's components share: one lock over every group.

:class:`SharedSessionState` owns the metadata lock and its condition, the
group and membership maps, the sandboxes a registration is creating, and the
input held for turns that have not started here. The supervisor constructs one
and hands that same object to each of its components, which read and change
these only under its lock. The lock protects in-memory ownership only: Docker,
marker and archive I/O run without it, and their results are revalidated under
it. The methods here are the checks and the bookkeeping more than one
component makes under that lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatLost,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.supervisor.group_lifecycle import GroupLifecycle, Operation

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from cloud_agent_host.drivers import DialectRuntime, PendingInput
    from cloud_agent_host.observe.guest_observer import (
        GuestObserverCursor,
    )
    from cloud_agent_host.observe.member_window_census import MemberWindow
    from cloud_agent_host.observe.tool_windows import ToolMarkReader
    from cloud_agent_host.resident_transport import (
        ResidentTransport,
        ResidentTurn,
    )
    from cloud_agent_host.supervisor.group_archive_policy import GroupArchivePolicy
    from cloud_agent_shared.sandbox_manager_wire import ContextStatus, ResidentState


class _SameRun(Enum):
    """A start that finishes one a request already admitted, under its fence."""

    SAME_RUN = "same_run"


_SAME_RUN: Final = _SameRun.SAME_RUN


@dataclass(frozen=True)
class SharedMemberManifest:
    """Durable fields needed to reconstruct one group membership: the wire's ``SharedMember``."""

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
    #: The installation snapshot the group's container was created from.
    environment_snapshot_id: str | None = None
    #: Why a snapshot handed for this group was not used: ``unavailable``
    #: (it could not be fetched or loaded here) or ``invalid`` (it does not
    #: match its record). The group was created from the sandbox image instead.
    environment_snapshot_failure: str | None = None


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
    #: A lifecycle check or final archive is queued, running, or needs retry.
    lifecycle_pending: bool = False


@dataclass(frozen=True, eq=False)
class _LostTurn:
    """A running turn whose process a residency loss closed.

    ``residency`` is the lease and the generation the loss named. The lease
    ends when this turn's boundary closes, by its stream or by the sweep.
    """

    turn: ResidentTurn[DialectRuntime]
    residency: tuple[str, int]
    lost_at: float


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
    #: The lease whose activation took on the member's process, by reusing it
    #: or by starting it, and that process's residency generation. Losing
    #: that residency ends the activation; losing one it is replacing, before
    #: it has taken on the new one, does not.
    lease_residency: tuple[str, int] | None = None
    #: The running turn whose process a residency loss closed, if any. Its
    #: activation ends with it.
    lost_turn: _LostTurn | None = field(default=None, repr=False)
    # Only the last completed activation can replay its acknowledgement.
    settlement_receipt: tuple[str, SharedSessionDescriptor] | None = field(default=None, repr=False)
    transport: ResidentTransport[DialectRuntime] | None = None
    current_turn: ResidentTurn[DialectRuntime] | None = None
    activation_processes: tuple[tuple[int, int], ...] | None = None
    observer_cursor: GuestObserverCursor | None = None
    tool_mark_reader: ToolMarkReader | None = None
    #: The member execution window this activation opened, if any. It is
    #: dropped wherever the cursor is dropped: a window belongs to exactly one
    #: activation, and a restarting member must never resume another one's.
    window: MemberWindow | None = None
    turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class _Group:
    spec: ChatSandboxSpec
    inspection: ChatSandboxInspection
    members: dict[str, _Member] = field(default_factory=dict)
    terminal_tokens: dict[str, str] = field(default_factory=dict)
    operation_tokens: dict[str, str] = field(default_factory=dict)
    snapshotting: bool = False
    #: Why the installation snapshot handed at creation was not used.
    environment_snapshot_failure: str | None = None
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
    #: The lifecycle generation an admission last proved the network in, and
    #: when that proof began (``shared_admission_proof``).
    network_proof: tuple[int, float] | None = None
    lifecycle_check: _LifecycleCheck | None = field(default=None, repr=False)
    idle_deadline: float | None = None
    fork_mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # The control plane's start fences. Every request that may start the
    # container names the fence it took, and a run is labelled with the
    # highest fence of the requests it actually admitted (``start_seq``), so
    # the checkpoint of its stop names only requests whose work it holds. A
    # request that is fenced but not yet here cannot lend its fence to another
    # request's run. ``None`` until a fenced request is admitted, as for a run
    # this host found already going after a restart.
    start_seq: int | None = None
    # A request without a fence was admitted to this run, as from an older
    # control plane. It counted no start, so the run's checkpoint names no
    # fence and never covers the group.
    run_unfenced: bool = False
    # The highest label of a run that ended (``start_seq`` as it stopped), or
    # of the last checkpoint receipt this host kept when it found the group
    # after a restart. That checkpoint may be the one the group moves with,
    # so no request under a fence at or below it may start the group again:
    # it changes the group after its checkpoint was taken.
    sealed_seq: int | None = None

    @property
    def lifecycle(self) -> GroupLifecycle:
        """The admission decisions over this group's lifecycle fields."""
        return GroupLifecycle(self)


@dataclass(frozen=True, eq=False)
class _LifecycleCheck:
    """Authority reserved while Docker or a durable marker is read without the lock."""

    group: _Group
    spec: ChatSandboxSpec
    container_id: str
    generation: int
    members: tuple[tuple[str, _Member], ...]


@dataclass(frozen=True)
class _HeldInput:
    """A message for an owner's turn that has not started on this sandbox manager yet."""

    owner: str
    pending: PendingInput
    held_at: float


class SharedSessionState:
    """The groups and members of one supervisor, and the one lock over them."""

    def __init__(
        self,
        manager: ChatSandboxManager,
        *,
        archive: GroupArchivePolicy,
        clock: Callable[[], float],
        idle_timeout_s: float,
    ) -> None:
        self.manager = manager
        #: The final-archive policy, fixed for the supervisor's lifetime.
        self.archive = archive
        self.archives_finals = archive.archives_finals
        self.clock = clock
        #: The one group inactivity timeout, changed only under :attr:`lock`.
        self.idle_timeout_s = idle_timeout_s
        self.groups: dict[str, _Group] = {}
        self.members: dict[str, _Group] = {}
        #: Sandboxes a registration is creating or adopting outside the lock.
        #: The group does not exist yet, so this set is their admission barrier.
        self.creating: set[str] = set()
        self.held_inputs: dict[str, list[_HeldInput]] = {}
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)

    @staticmethod
    def archive_members_locked(group: _Group) -> tuple[SharedMemberManifest, ...]:
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
    def mark_container_lost_locked(group: _Group) -> None:
        """Make a same-name replacement permanently inadmissible to this group."""
        if not group.container_lost:
            group.lifecycle_generation += 1
        group.container_lost = True
        group.idle_deadline = None
        for member in group.members.values():
            member.context_status = "lost"
            member.resident_state = "lost"

    def accept_inspection_locked(
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
            self.mark_container_lost_locked(group)
            raise RetainedChatLost("shared sandbox container identity changed")
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        group.inspection = inspection
        if group.initial_archive_preservation_pending and not inspection.running:
            group.initial_archive_preservation_pending = False
            group.snapshotting = False
            self.condition.notify_all()
        return inspection

    def begin_lifecycle_check_locked(self, group: _Group) -> _LifecycleCheck:
        """Reserve this group, leaving the metadata lock free during external I/O."""
        group.lifecycle.admit(Operation.LIFECYCLE_CHECK)
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

    def require_lifecycle_check_locked(self, check: _LifecycleCheck) -> None:
        """Reject results whose group, members or lifecycle authority was superseded."""
        group = check.group
        if (
            self.groups.get(check.spec.sandbox_id) is not group
            or group.spec != check.spec
            or group.inspection.container_id != check.container_id
            or group.lifecycle_generation != check.generation
            or group.lifecycle_check is not check
            or len(group.members) != len(check.members)
            or any(
                group.members.get(thread_id) is not member
                or self.members.get(thread_id) is not group
                for thread_id, member in check.members
            )
        ):
            raise SessionLeaseLost("shared sandbox lifecycle changed during observation")

    def end_lifecycle_check_locked(self, check: _LifecycleCheck) -> None:
        """Release only this operation's reservation, including on failure."""
        if check.group.lifecycle_check is check:
            check.group.lifecycle_check = None
            check.group.lifecycle_generation += 1
            self.condition.notify_all()

    def require_current_inspection(self, group: _Group) -> ChatSandboxInspection:
        """Inspect and accept only the exact Docker object owned by ``group``."""
        current = self.manager.inspect(group.spec.container_name)
        with self.lock:
            if current is None:
                self.mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            return self.accept_inspection_locked(group, current)

    def wait_while_locked(self, busy: Callable[[], str | None], *, deadline: float) -> None:
        """Wait on the condition while ``busy`` names a transition still in progress.

        Gives up with that name once ``deadline`` passes. The lock is released
        while waiting, so the caller rechecks anything it read before.
        """
        while (reason := busy()) is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionBusy(reason)
            self.condition.wait(timeout=min(remaining, 0.5))

    def wait_for_start_locked(self, group: _Group, *, retry_failed: bool = False) -> None:
        """Wait for an in-flight bridge check without holding the metadata lock.

        Workspace reads and terminal opens revalidate even a running container.
        Their temporary start fence must not reject an already leased member's
        prompt. Callers recheck membership and activation after this wait.
        """
        self.wait_while_locked(
            lambda: group.lifecycle.busy_reason(Operation.START_WAIT),
            deadline=time.monotonic() + 30.0,
        )
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        if group.start_failed and not retry_failed:
            raise SandboxError("shared workspace start verification failed")

    @staticmethod
    def join_run_locked(group: _Group, start_seq: int | _SameRun | None) -> None:
        """Count a request admitted while the group runs in that run's label.

        Called where the request is admitted, under the lock that keeps the
        group from stopping before its work is done. A request without a
        fence leaves the run unlabelled, so its checkpoint covers nothing.

        A run with no label yet (one found going after a restart) takes the
        request's: every request it ran before took a lower fence, and one
        that arrives after its stop is refused as sealed.
        """
        if start_seq is _SAME_RUN:
            return
        group.lifecycle.admit(Operation.JOIN_RUN)
        if start_seq is None:
            group.run_unfenced = True
        elif group.start_seq is None or start_seq > group.start_seq:
            group.start_seq = start_seq

    def arm_idle_if_quiescent(
        self,
        group: _Group,
        *,
        allow_lifecycle_check: bool = False,
        last_activity: float | None = None,
    ) -> None:
        """Arm the idle deadline from ``last_activity``, or from now, if nothing holds it off.

        A lost group's last observation may still say running, but nothing
        stops it again: armed, the sweep would pick it on every pass.
        """
        if (
            group.lifecycle.is_quiescent(allow_lifecycle_check=allow_lifecycle_check)
            and group.inspection.running
            and not group.container_lost
            and not group.stop_archive_pending
            and not group.stop_archive_blocked
            and not group.orphan_finalization_pending
            and not group.start_archive_completion_pending
        ):
            idle_from = self.clock() if last_activity is None else last_activity
            group.idle_deadline = idle_from + self.idle_timeout_s
        else:
            group.idle_deadline = None

    def member(self, thread_id: str) -> tuple[_Group, _Member]:
        """The group a thread is a member of, and its member; the caller holds the lock."""
        group = self.members.get(thread_id)
        if group is None:
            raise SandboxError(f"chat {thread_id!r} has no shared sandbox membership")
        return group, group.members[thread_id]

    def leased_member(self, thread_id: str, token: str) -> tuple[_Group, _Member]:
        """As :meth:`member`, only while ``token`` is the member's activation lease."""
        group, member = self.member(thread_id)
        if not token or member.lease_token != token:
            raise SessionLeaseLost("shared member activation lease is stale")
        return group, member

    def descriptors(self, group: _Group) -> builtins.list[SharedSessionDescriptor]:
        """Describe every member of ``group``; the caller holds the lock."""
        return [self.descriptor(group, member) for member in group.members.values()]

    @staticmethod
    def descriptor(
        group: _Group,
        member: _Member,
        *,
        resident_action: str = "unknown",
        stages: builtins.list[dict[str, Any]] | None = None,
        call_id: str | None = None,
        recorded: bool = False,
        truncated: bool = False,
    ) -> SharedSessionDescriptor:
        """Describe one member with its group's Docker and lifecycle state."""
        return SharedSessionDescriptor(
            thread_id=member.thread_id,
            origin_thread_id=group.spec.thread_id,
            sandbox_id=group.spec.sandbox_id,
            generation=group.spec.generation,
            container_id=group.inspection.container_id,
            container_name=group.inspection.name,
            docker_status=group.inspection.status.value,
            running=group.inspection.running,
            group_busy=not group.lifecycle.is_quiescent(),
            lifecycle_pending=(
                group.lifecycle_check is not None
                or group.stopping
                or group.stop_archive_pending
                or group.orphan_finalization_pending
            ),
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


__all__ = [
    "SharedMemberManifest",
    "SharedSessionDescriptor",
    "SharedSessionLease",
    "SharedSessionState",
]
