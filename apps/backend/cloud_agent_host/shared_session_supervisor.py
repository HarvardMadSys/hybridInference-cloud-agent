"""Concurrent member supervisor for one shared retained sandbox workspace.

:class:`SharedChatSessionSupervisor` is the entry point the sandbox manager,
the runner and the tests use. It owns the background threads and constructs
one :class:`~cloud_agent_host.shared_session_state.SharedSessionState`, the
groups, members and metadata lock, which it hands to each of its components.
Each public call is delegated to the component that implements it:

==========================================  =====================================================
Component                                   Responsibility
==========================================  =====================================================
``group_lifecycle_controller``              Start, stop, network fail-stop, archive deletion
``group_registration``                      Manifests: create, adopt or restore a group
``group_reconciliation``                    Discover and recover containers; observe groups
``member_sessions``                         Member leases, resident processes, turns, settlement
``native_session_fork``                     Native session forks
``group_environment``                       Environment preparation
``initial_archive_capture``                 The pre-prompt workspace archive
``member_window_sampler``                   Member execution window censuses and checkpoints
``workspace_references``                    Terminal and workspace-operation references
==========================================  =====================================================
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.archive_failure_diagnostics import (
    _safe_error_type,
    archive_failure_http_detail,
)
from cloud_agent_host.group_archive_policy import GroupArchivePolicy, NullArchivePolicy
from cloud_agent_host.group_environment import GroupEnvironmentPreparation
from cloud_agent_host.group_lifecycle_controller import GroupLifecycleController
from cloud_agent_host.group_reconciliation import GroupReconciler
from cloud_agent_host.group_registration import GroupRegistration
from cloud_agent_host.initial_archive_capture import InitialArchiveCapture
from cloud_agent_host.member_resources import (
    MEMBER_WINDOW_SAMPLE_INTERVAL_MS,
    RUNNING_CHECKPOINT_INTERVAL_S,
)
from cloud_agent_host.member_sessions import MemberSessions
from cloud_agent_host.member_window_sampler import WindowSampler
from cloud_agent_host.native_session_fork import ForkService, NativeForker
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransportFactory,
    ResidentTurn,
)
from cloud_agent_host.session_lifecycle import (
    DEFAULT_CHAT_IDLE_TIMEOUT_S,
    DEFAULT_SWEEP_INTERVAL_S,
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.shared_session_state import (
    _SAME_RUN,
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionLease,
    SharedSessionState,
    _SameRun,
)
from cloud_agent_host.workspace_references import WorkspaceReferences

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from cloud_agent_host.chat_sandbox import (
        ChatSandboxManager,
        ChatSandboxSpec,
        RetainedChatBackend,
        SandboxActionFacts,
    )
    from cloud_agent_host.drivers import DialectRuntime, InputOutcome
    from cloud_agent_host.member_sessions import SharedTurnTelemetry
    from cloud_agent_host.native_session_fork import NativeSessionForkResult
    from cloud_agent_host.sandbox_checkpoints import CheckpointRestore
    from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef
    from cloud_agent_shared.operation_profiling import (
        StageRecorder,
    )
    from cloud_agent_shared.sandbox_manager_wire import ContextStatus

logger = logging.getLogger(__name__)


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
        archive_policy: GroupArchivePolicy | None = None,
    ) -> None:
        if idle_timeout_s <= 0 or sweep_interval_s <= 0:
            raise ValueError("shared sandbox timeout values must be positive")
        #: The census period. One second is what the summary reports and what
        #: a reader compares across turns, so the sampler owns it.
        sample_interval_s = window_sample_interval_s if window_sample_interval_s > 0 else 1.0
        self._sample_tick_s = min(0.25, sample_interval_s / 4)
        self._sampler_thread: threading.Thread | None = None
        self._manager = manager
        self._sweep_interval_s = sweep_interval_s
        transport_factory = transport_factory or ResidentTransportFactory(manager)
        # The final archive runs only after Docker has cleanly stopped the
        # exact generation, while a lifecycle barrier still blocks concurrent
        # workspace writes. It is synchronous so success means the durable
        # final object exists; failure remains pending and prevents the group
        # from restarting. The policy is fixed for the supervisor's lifetime.
        self._archive = archive_policy if archive_policy is not None else NullArchivePolicy()
        self._state = SharedSessionState(
            manager, archive=self._archive, clock=clock, idle_timeout_s=idle_timeout_s
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle = GroupLifecycleController(self._state)
        self._registration = GroupRegistration(self._state)
        self._reconciler = GroupReconciler(self._state, lifecycle=self._lifecycle)
        self._recovery = self._reconciler.recovery
        self._sessions = MemberSessions(
            self._state,
            lifecycle=self._lifecycle,
            registration=self._registration,
            transport_factory=transport_factory,
            sample_interval_s=sample_interval_s,
            checkpoint_interval_s=max(0.0, checkpoint_interval_s),
        )
        self._forks = ForkService(
            self._state,
            lifecycle=self._lifecycle,
            sessions=self._sessions,
            transport_factory=transport_factory,
            native_forker=native_forker or transport_factory.fork_native,
        )
        self._environment = GroupEnvironmentPreparation(self._state, lifecycle=self._lifecycle)
        self._initial_archive = InitialArchiveCapture(self._state, lifecycle=self._lifecycle)
        self._windows = WindowSampler(self._state)
        self._references = WorkspaceReferences(self._state, lifecycle=self._lifecycle)

    @property
    def state(self) -> SharedSessionState:
        """The groups, the members and the one metadata lock every component shares."""
        return self._state

    @property
    def initial_archive_wait_s(self) -> float:
        """How long the pre-prompt archive waits for a concurrent workspace request."""
        return self._initial_archive.initial_archive_wait_s

    @initial_archive_wait_s.setter
    def initial_archive_wait_s(self, value: float) -> None:
        self._initial_archive.initial_archive_wait_s = value

    @property
    def idle_timeout_s(self) -> float:
        """Return the one group inactivity timeout."""
        return self._state.idle_timeout_s

    def set_idle_timeout_s(self, idle_timeout_s: float) -> None:
        """Change the inactivity timeout at runtime, as the admin page asks.

        A group's deadline is armed when it goes quiet, so the new value
        applies from each group's next quiet moment; a deadline already armed
        keeps the value it was armed with rather than moving under a group that
        is about to be stopped.
        """
        if idle_timeout_s <= 0:
            raise ValueError("shared sandbox timeout values must be positive")
        with self._state.lock:
            self._state.idle_timeout_s = idle_timeout_s

    @property
    def archive_policy(self) -> GroupArchivePolicy:
        """The archive policy this supervisor was constructed with."""
        return self._archive

    def start(self) -> None:
        """Reconstruct shared containers and start the group idle sweep."""
        with self._state.lock:
            if self._thread is not None and self._thread.is_alive():
                return
        # Reconciliation may synchronously upload a final archive.  Do not
        # retain the supervisor-wide metadata lock while that bounded I/O is
        # in flight: unrelated status calls must remain responsive.
        self._recovery.start()
        self.reconcile()
        with self._state.lock:
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
        self._recovery.quiesce()
        with self._state.lock:
            self._thread = None
            self._sampler_thread = None

    def close(self) -> None:
        """Stop the supervisor process; leave Docker-owned state intact."""
        self.quiesce()
        # Pipes cannot be reattached by a replacement supervisor. Close them
        # explicitly; native locators remain in Docker and are resumed later.
        with self._state.lock:
            transports = [
                member.transport
                for group in self._state.groups.values()
                for member in group.members.values()
                if member.transport is not None
            ]
            observations = [
                (group.spec, member.observer_cursor)
                for group in self._state.groups.values()
                for member in group.members.values()
                if member.observer_cursor is not None
            ]
            turn_locks = [
                member.turn_lock
                for group in self._state.groups.values()
                for member in group.members.values()
                # An initialization may also hold this lock, but its context
                # manager is still alive and owns the release. Only a detached
                # admitted turn loses the normal ``complete_turn`` release
                # path when ``close`` clears ``current_turn`` below.
                if member.current_turn is not None and member.turn_lock.locked()
            ]
            for group in self._state.groups.values():
                for member in group.members.values():
                    member.transport = None
                    member.current_turn = None
                    member.activation_processes = None
                    member.observer_cursor = None
                    member.tool_mark_reader = None
                    member.window = None
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

    def sweep(self) -> builtins.list[SharedSessionDescriptor]:
        """Stop groups whose single refreshed inactivity deadline elapsed.

        Each pass first closes the turns :meth:`residency_lost` left to a
        stream that has not closed them within ``LOST_TURN_GRACE_S``.
        """
        self._sessions.close_lost_turns()
        now = self._state.clock()
        with self._state.lock:
            candidates = [
                (next(iter(group.members)), group.spec.generation)
                for group in self._state.groups.values()
                if group.inspection.running
                and group.idle_deadline is not None
                and group.idle_deadline <= now
                and group.lifecycle.is_quiescent()
            ]
        stopped: builtins.list[SharedSessionDescriptor] = []
        for thread_id, generation in candidates:
            try:
                with contextlib.suppress(SessionBusy, SessionLeaseLost):
                    stopped.append(
                        self.stop_if_idle(
                            thread_id,
                            generation=generation,
                            require_expired_deadline=True,
                        )
                    )
            except Exception as exc:
                # One group that cannot be stopped must not keep the groups
                # after it running. Only the error type is logged: an archive
                # provider's message can carry what a log must not.
                logger.error(
                    "idle shared sandbox could not be stopped",
                    extra={
                        "event": "shared_chat_idle_stop_failed",
                        "thread_id": thread_id,
                        "generation": generation,
                        "error_type": _safe_error_type(exc),
                    },
                )
        return stopped

    def _sample_loop(self) -> None:
        """Census every open window once a second until the supervisor stops."""
        while not self._stop.wait(self._sample_tick_s):
            with contextlib.suppress(Exception):
                self.sample_open_windows()

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self._sweep_interval_s):
            with contextlib.suppress(Exception):
                self.guard_network()
            with contextlib.suppress(Exception):
                self.sweep()

    def stop_all_for_shutdown(self, *, include_running: bool = True) -> None:
        """Cleanly stop and archive every running group during sandbox manager shutdown."""
        return self._lifecycle.stop_all_for_shutdown(include_running=include_running)

    def shutdown_archive_candidate_count(self) -> int:
        """Return how many groups shutdown must stop or finish archiving."""
        return self._lifecycle.shutdown_archive_candidate_count()

    def has_pending_archive_work(self) -> bool:
        """Whether shutdown must preserve or finish an older archive lifecycle."""
        return self._lifecycle.has_pending_archive_work()

    def reconcile(
        self, *, background_recovery: bool = False
    ) -> builtins.list[SharedSessionDescriptor]:
        """Discover containers, fencing each group's slow recovery independently."""
        return self._reconciler.reconcile(background_recovery=background_recovery)

    def register_manifest(
        self,
        spec: ChatSandboxSpec,
        members: builtins.list[SharedMemberManifest],
        *,
        expected_container_id: str | None = None,
        facts: SandboxActionFacts | None = None,
        observer: StageRecorder | None = None,
        restore: CheckpointRestore | None = None,
        environment_snapshot: EnvironmentSnapshotRef | None = None,
    ) -> builtins.list[SharedSessionDescriptor]:
        """Install the control plane's authoritative member aliases."""
        return self._registration.register_manifest(
            spec,
            members,
            expected_container_id=expected_container_id,
            facts=facts,
            observer=observer,
            restore=restore,
            environment_snapshot=environment_snapshot,
        )

    def start_seq_for(self, sandbox_id: str) -> int | None:
        """The start fence of the run a stop of this group ends, if known."""
        return self._lifecycle.start_seq_for(sandbox_id)

    def owns(self, thread_id: str) -> bool:
        """Whether this supervisor currently knows the membership alias."""
        return self._registration.owns(thread_id)

    def admin_deletion_target_absent(
        self,
        thread_id: str,
        *,
        sandbox_id: str,
        container_id: str,
        on_absent: Callable[[str], None] | None = None,
    ) -> bool:
        """Prove a stale sandbox absent, optionally fencing an absence callback."""
        return self._registration.admin_deletion_target_absent(
            thread_id, sandbox_id=sandbox_id, container_id=container_id, on_absent=on_absent
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
        start_seq: int | None = None,
        restore: CheckpointRestore | None = None,
        environment_snapshot: EnvironmentSnapshotRef | None = None,
    ) -> SharedSessionLease:
        """Start one group if needed and lease only the addressed member."""
        return self._sessions.acquire(
            spec,
            thread_id=thread_id,
            owner=owner,
            activation_generation=activation_generation,
            has_settled_context=has_settled_context,
            manifest=manifest,
            expected_container_id=expected_container_id,
            profiling=profiling,
            start_seq=start_seq,
            restore=restore,
            environment_snapshot=environment_snapshot,
        )

    def ensure_resident(
        self,
        thread_id: str,
        token: str,
        launch: ResidentLaunch,
        profiling: dict[str, Any] | None = None,
    ) -> SharedSessionDescriptor:
        """Open or reuse the member transport after workspace preparation."""
        return self._sessions.ensure_resident(thread_id, token, launch, profiling)

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
        return self._sessions.start_turn(
            thread_id, token, prompt=prompt, model=model, reasoning_effort=reasoning_effort
        )

    def complete_turn(
        self,
        thread_id: str,
        token: str,
        turn: ResidentTurn[DialectRuntime],
    ) -> SharedTurnTelemetry:
        """Close observation, then enforce and release the activation boundary."""
        return self._sessions.complete_turn(thread_id, token, turn)

    def sample_open_windows(self) -> int:
        """Take one census for every member window that is due for one."""
        return self._windows.sample_open_windows()

    def take_reported_checkpoint(
        self, thread_id: str, turn: ResidentTurn[DialectRuntime]
    ) -> dict[str, Any] | None:
        """The newest checkpoint of ``turn``'s window not yet handed out, or None."""
        return self._windows.take_reported_checkpoint(thread_id, turn)

    def checkpoint_running_window(
        self,
        thread_id: str,
        token: str,
    ) -> dict[str, Any] | None:
        """Build one cumulative checkpoint for a member's running turn."""
        return self._windows.checkpoint_running_window(thread_id, token)

    def interrupt(self, thread_id: str, token: str) -> None:
        """Interrupt the exact member activation, leaving siblings untouched."""
        return self._sessions.interrupt(thread_id, token)

    def inject_input(
        self, thread_id: str, *, owner: str, message_id: str, text: str
    ) -> InputOutcome:
        """Hand the owner's message to this member's turn, running or about to."""
        return self._sessions.inject_input(thread_id, owner=owner, message_id=message_id, text=text)

    def pending_questions(self, thread_id: str, *, owner: str) -> builtins.list[dict[str, Any]]:
        """Read only questions belonging to the control plane's active attempt."""
        return self._sessions.pending_questions(thread_id, owner=owner)

    def answer_question(
        self,
        thread_id: str,
        *,
        owner: str,
        request_id: str,
        answers: dict[str, builtins.list[str]],
        reject: bool = False,
    ) -> InputOutcome:
        """Answer the owner's current turn; never retain replies for another turn."""
        return self._sessions.answer_question(
            thread_id, owner=owner, request_id=request_id, answers=answers, reject=reject
        )

    def abort_turn(
        self,
        thread_id: str,
        token: str,
        turn: ResidentTurn[DialectRuntime],
    ) -> SharedTurnTelemetry:
        """Fence a turn whose sandbox manager stream disappeared before its result."""
        return self._sessions.abort_turn(thread_id, token, turn)

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
        return self._sessions.settle(
            thread_id,
            token,
            job_id=job_id,
            context_status=context_status,
            native_session_id=native_session_id,
        )

    def abandon(
        self,
        thread_id: str,
        token: str,
        *,
        lost: bool,
        residency: tuple[str, int] | None = None,
    ) -> SharedSessionDescriptor:
        """Release one failed member; restart/lose it without killing siblings."""
        return self._sessions.abandon(thread_id, token, lost=lost, residency=residency)

    def residency_lost(
        self,
        thread_id: str,
        *,
        generation: int,
    ) -> SharedSessionDescriptor:
        """Close only the process whose renewable grant fence was rejected."""
        return self._sessions.residency_lost(thread_id, generation=generation)

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
        start_seq: int | None = None,
    ) -> NativeSessionForkResult:
        """Fork immediately, then prove the child live before returning."""
        return self._forks.fork_native(
            source_thread_id,
            operation_id=operation_id,
            source_native_session_id=source_native_session_id,
            source_native_turn_no=source_native_turn_no,
            target_thread_id=target_thread_id,
            target_slot_id=target_slot_id,
            source_launch=source_launch,
            target_launch=target_launch,
            start_seq=start_seq,
        )

    def operation_opened(
        self, thread_id: str, *, start_seq: int | None = None
    ) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one shared workspace operation without serializing it."""
        return self._references.operation_opened(thread_id, start_seq=start_seq)

    def prepare_environment(
        self,
        thread_id: str,
        environment: Any,
        preparer: Any,
        *,
        activation_owner: str | None = None,
        start_seq: int | _SameRun | None = _SAME_RUN,
    ) -> dict[str, Any]:
        """Serialize preparation with writers, preserving this activation's lease."""
        return self._environment.prepare_environment(
            thread_id, environment, preparer, activation_owner=activation_owner, start_seq=start_seq
        )

    def cancel_environment(self, thread_id: str, activation_owner: str) -> None:
        """Only the preparing activation may revoke its long-running setup."""
        return self._environment.cancel_environment(thread_id, activation_owner)

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
        """Capture the pre-prompt workspace owned by one fresh activation."""
        return self._initial_archive.capture_initial_archive(
            thread_id,
            token,
            expected_spec=expected_spec,
            expected_container_id=expected_container_id,
            capture=capture,
        )

    def terminal_opened(
        self, thread_id: str, *, start_seq: int | None = None
    ) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one member-owned PTY against the shared group."""
        return self._references.terminal_opened(thread_id, start_seq=start_seq)

    def terminal_closed(
        self,
        thread_id: str,
        token: str,
        *,
        last_activity_monotonic: float | None = None,
    ) -> SharedSessionDescriptor | None:
        """Release one PTY and refresh the group's single activity clock."""
        return self._references.terminal_closed(
            thread_id, token, last_activity_monotonic=last_activity_monotonic
        )

    def operation_closed(self, thread_id: str, token: str) -> SharedSessionDescriptor | None:
        """Release one workspace operation and refresh the group activity clock."""
        return self._references.operation_closed(thread_id, token)

    def backend(
        self, thread_id: str, *, network_proved_at: float | None = None
    ) -> RetainedChatBackend:
        """Return the group's one backend; its commands may stand on an admission's proof."""
        return self._references.backend(thread_id, network_proved_at=network_proved_at)

    def status(self, thread_id: str) -> SharedSessionDescriptor:
        """Return fresh Docker and member process state."""
        return self._reconciler.status(thread_id)

    def list(self, *, background_recovery: bool = False) -> builtins.list[SharedSessionDescriptor]:
        """Observe memberships; HTTP readers queue slow lifecycle recovery."""
        return self._reconciler.list(background_recovery=background_recovery)

    def list_for_thread(self, thread_id: str) -> builtins.list[SharedSessionDescriptor]:
        """Observe one known group without running host-wide reconciliation."""
        return self._reconciler.list_for_thread(thread_id)

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
        """Close all member transports, then stop the group exactly once."""
        return self._lifecycle.stop_if_idle(
            thread_id,
            generation=generation,
            activation_generation=activation_generation,
            reason=reason,
            member_activation_generations=member_activation_generations,
            require_expired_deadline=require_expired_deadline,
            defer_archive=defer_archive,
        )

    def begin_archive(
        self,
        thread_id: str,
        *,
        request_id: str,
        generation: int,
        container_id: str,
    ) -> tuple[ChatSandboxSpec, dict[str, Any]]:
        """Hold a stopped group's admission barrier across S3 verification."""
        return self._lifecycle.begin_archive(
            thread_id, request_id=request_id, generation=generation, container_id=container_id
        )

    def abort_archive(self, spec: ChatSandboxSpec, *, request_id: str, container_id: str) -> None:
        """Release only the caller's archival barrier after a failed operation."""
        return self._lifecycle.abort_archive(spec, request_id=request_id, container_id=container_id)

    def commit_archive(self, spec: ChatSandboxSpec, *, request_id: str, container_id: str) -> None:
        """Remove a stopped group after durable authorization; cleanup is replayable."""
        return self._lifecycle.commit_archive(
            spec, request_id=request_id, container_id=container_id
        )

    def guard_network(self) -> builtins.list[SharedSessionDescriptor]:
        """Fail-stop every running workload when the host cannot prove its rules."""
        return self._lifecycle.guard_network()


__all__ = [
    "SharedChatSessionSupervisor",
    "archive_failure_http_detail",
]
