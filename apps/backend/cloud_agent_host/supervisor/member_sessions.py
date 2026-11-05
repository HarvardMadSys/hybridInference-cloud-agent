"""One member's activation: its lease, its resident CLI process and its turns.

Each member of a group is leased, runs its turns and settles or is abandoned
independently of its siblings. A member's own turn lock serializes its process
launch and its running turn; the metadata lock guards only the bookkeeping
around them, which is rechecked after every call made without it.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn

from cloud_agent_host.archive.archive_failure_diagnostics import (
    _safe_error_type,
)
from cloud_agent_host.container.chat_sandbox import (
    MEMBER_UID_BASE,
    ChatSandboxSpec,
    RetainedChatLost,
    SandboxActionFacts,
)
from cloud_agent_host.container.sandbox import KATA_RUNTIME, SandboxError
from cloud_agent_host.drivers import DialectRuntime, InputOutcome, PendingInput
from cloud_agent_host.observe.guest_observer import (
    GUEST_OBSERVER_SOURCE,
    GuestObservation,
    GuestObserverCursor,
)
from cloud_agent_host.resident_probe import probe_refusal_message
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.supervisor.group_lifecycle import Operation
from cloud_agent_host.supervisor.shared_session_state import (
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionLease,
    SharedSessionState,
    _Group,
    _HeldInput,
    _LostTurn,
    _Member,
)
from cloud_agent_host.supervisor.turn_arming import TurnFences, arm_turn
from cloud_agent_shared.environment_snapshots import SNAPSHOT_LABEL, EnvironmentSnapshotRef
from cloud_agent_shared.operation_profiling import (
    StageRecorder,
    context_call_id,
    start_stage_recording,
    substage,
)
from cloud_agent_shared.shared_protocol import INPUT_TURN_ENDED

if TYPE_CHECKING:
    import builtins

    from cloud_agent_host.archive.sandbox_checkpoints import CheckpointRestore
    from cloud_agent_host.observe.member_window_census import MemberWindow
    from cloud_agent_host.observe.tool_windows import ToolMark
    from cloud_agent_host.resident_transport import (
        ResidentLaunch,
        ResidentTransport,
        ResidentTransportFactory,
        ResidentTurn,
    )
    from cloud_agent_host.supervisor.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.supervisor.group_registration import GroupRegistration
    from cloud_agent_shared.sandbox_manager_wire import ContextStatus

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


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


#: How long a message sent into a turn that has not started here is kept for
#: it. Long past any sandbox preparation; a turn that never starts leaves its
#: message to the control plane, which sends it as the next turn instead.
HELD_INPUT_TTL_S = 15 * 60.0
#: How long a turn whose process a residency loss closed is left to its stream
#: before the sweep closes it. A stream that is still read ends such a turn
#: within seconds; one whose runner stopped reading never reaches its end.
LOST_TURN_GRACE_S = 60.0


class MemberSessions:
    """Lease members, run their turns and settle or abandon each one."""

    def __init__(
        self,
        state: SharedSessionState,
        *,
        lifecycle: GroupLifecycleController,
        registration: GroupRegistration,
        transport_factory: ResidentTransportFactory,
        sample_interval_s: float,
        checkpoint_interval_s: float,
    ) -> None:
        self._state = state
        self._manager = state.manager
        self._lifecycle = lifecycle
        self._registration = registration
        self._transport_factory = transport_factory
        #: The member window census period (see :class:`WindowSampler`).
        self._sample_interval_s = sample_interval_s
        self._checkpoint_interval_s = checkpoint_interval_s

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
        # Container actions are always reported: they are a couple of string
        # assignments, and the runner needs them whenever it is profiling.
        # Durations are collected only for a selection this process can honour:
        # its own switch, its own run, and the caller's inherited deadline.
        facts = SandboxActionFacts()
        recorder = start_stage_recording(profiling)
        call_id = context_call_id(profiling)
        self._registration.register_manifest(
            spec,
            manifest,
            expected_container_id=expected_container_id,
            facts=facts,
            observer=recorder,
            restore=restore,
            environment_snapshot=environment_snapshot,
        )
        with self._state.lock:
            group, member = self._state.member(thread_id)
            self._state.wait_while_locked(
                lambda: group.lifecycle.busy_reason(Operation.ACTIVATION_WAIT),
                deadline=time.monotonic() + 30.0,
            )
            group.lifecycle.admit(Operation.RUN)
            if member.context_status == "lost":
                raise RetainedChatLost("shared member context was lost")
            existing = self._owner_lease_locked(
                thread_id,
                group,
                member,
                owner=owner,
                facts=facts,
                recorder=recorder,
                call_id=call_id,
            )
            if existing is not None:
                return existing
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
            self._lose_missing_container(thread_id, group, member, missing=current is None)

        inspection = self._lifecycle.start_and_prove(
            group,
            start_seq=start_seq,
            facts=facts,
            observer=recorder,
        )
        warm_observer = getattr(self._manager, "warm_guest_observer", None)
        if warm_observer is not None and facts.container_action in ("create", "start"):
            # This activation booted the guest and proved it: its observer
            # starts now, beside the member's preparation, not in the prompt.
            warm_observer(group.spec, inspection.container_id)
        with self._state.lock:
            current_group = self._state.members.get(thread_id)
            current_member = (
                current_group.members.get(thread_id) if current_group is not None else None
            )
            if (
                current_group is not group
                or current_member is not member
                or group.lifecycle_generation != lifecycle_generation
            ):
                raise SessionLeaseLost("shared sandbox lifecycle changed during activation")
            group.lifecycle.admit(Operation.MUTATION)
            existing = self._owner_lease_locked(
                thread_id,
                group,
                member,
                owner=owner,
                facts=facts,
                recorder=recorder,
                call_id=call_id,
            )
            if existing is not None:
                return existing
            if activation_generation < member.activation_generation:
                raise SessionLeaseLost("member activation generation is stale")
            if expected_container_id and inspection.container_id != expected_container_id:
                member.context_status = "lost"
                member.resident_state = "lost"
                raise RetainedChatLost("shared sandbox container identity changed")
            group.inspection = inspection
            self._state.join_run_locked(group, start_seq)
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

    def _owner_lease_locked(
        self,
        thread_id: str,
        group: _Group,
        member: _Member,
        *,
        owner: str,
        facts: SandboxActionFacts,
        recorder: StageRecorder | None,
        call_id: str | None,
    ) -> SharedSessionLease | None:
        """Return the owner's existing lease, refuse a busy member, else None.

        The caller holds the lock.
        """
        if member.lease_token is not None:
            if member.lease_owner == owner:
                return self._lease(group, member, facts=facts, recorder=recorder, call_id=call_id)
            raise SessionBusy(f"chat {thread_id!r} already has an active turn")
        if member.current_turn is not None:
            raise SessionBusy(f"chat {thread_id!r} already has a running turn")
        return None

    def _lose_missing_container(
        self, thread_id: str, group: _Group, member: _Member, *, missing: bool
    ) -> NoReturn:
        """Mark a still-current member lost and refuse its missing or replaced container."""
        with self._state.lock:
            if (
                self._state.members.get(thread_id) is group
                and group.members.get(thread_id) is member
            ):
                member.context_status = "lost"
                member.resident_state = "lost"
        if missing:
            raise RetainedChatLost("shared sandbox container is missing")
        raise RetainedChatLost("shared sandbox container identity changed")

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
        with self._state.lock:
            group, member = self._state.leased_member(thread_id, token)
            self._state.wait_for_start_locked(group)
            group, member = self._state.leased_member(thread_id, token)
            group.lifecycle.admit(Operation.MUTATION)
        # CLI initialization may block. The member lock, not the group lock,
        # leaves siblings able to initialize and execute simultaneously. The
        # lease is checked again around the launch: an activation abandoned
        # while it waited or launched must not install its process.
        with member.turn_lock:
            resident_action = self.ensure_transport(
                group, member, launch, observer=recorder, lease_token=token
            )
        with self._state.lock:
            return self._state.descriptor(
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
        with self._state.lock:
            group, member = self._state.leased_member(thread_id, token)
            self._state.wait_for_start_locked(group)
            group, member = self._state.leased_member(thread_id, token)
            group.lifecycle.admit(Operation.MUTATION)
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
        fences: TurnFences | None = None
        try:
            # The census and every fence complete here, before admission is
            # rechecked and so before the prompt is written (turn_arming).
            fences = arm_turn(
                self._manager,
                group.spec,
                slot_id=member.resident_slot_id,
                runtime_id=transport.launch.runtime_id,
                sample_interval_s=self._sample_interval_s,
            )
            with self._state.lock:
                self._state.wait_for_start_locked(group)
                group.lifecycle.admit(Operation.MUTATION)
                current_group = self._state.members.get(thread_id)
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
            if fences is not None:
                fences.close_unadmitted(self._manager, group.spec)
            member.turn_lock.release()
            raise
        admitted = False
        held_inputs: list[PendingInput] = []
        with self._state.lock:
            current_group = self._state.members.get(thread_id)
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
                member.activation_processes = fences.activation_processes
                member.observer_cursor = fences.observer_cursor
                member.tool_mark_reader = fences.tool_mark_reader
                member.window = fences.member_window(
                    self._manager, group.spec, checkpoint_interval_s=self._checkpoint_interval_s
                )
        if not admitted:
            self._unwind_unadmitted_turn(group, member, turn, fences)
            raise SessionLeaseLost("shared sandbox lifecycle changed during turn admission")
        for pending in held_inputs:
            turn.inject(pending.text, message_id=pending.message_id)
        return turn

    def _unwind_unadmitted_turn(
        self,
        group: _Group,
        member: _Member,
        turn: ResidentTurn[DialectRuntime],
        fences: TurnFences,
    ) -> None:
        """Stop a started turn admission refused, then release its member lock."""
        with contextlib.suppress(Exception):
            turn.interrupt()
        fences.close_unadmitted(self._manager, group.spec)
        with contextlib.suppress(Exception):
            self._manager.reap_member_processes_except(
                group.spec,
                slot_id=member.resident_slot_id,
                user_uid=MEMBER_UID_BASE + member.resident_slot_id,
                user_gid=self._manager.backend.gid,
                baseline=fences.activation_processes,
            )
        member.turn_lock.release()

    def _take_held_inputs(self, thread_id: str, owner: str | None) -> list[PendingInput]:
        """Remove and return what was held for ``owner``'s turn; drop the rest."""
        now = self._state.clock()
        return [
            held.pending
            for held in self._state.held_inputs.pop(thread_id, [])
            if held.owner == owner and now - held.held_at < HELD_INPUT_TTL_S
        ]

    def complete_turn(
        self,
        thread_id: str,
        token: str,
        turn: ResidentTurn[DialectRuntime],
    ) -> SharedTurnTelemetry:
        """Close observation, then enforce and release the activation boundary.

        A turn whose process a residency loss closed takes its activation with
        it, as ``abandon(lost=True)`` would: see :meth:`residency_lost`.
        """
        with self._state.lock:
            group, member = self._state.leased_member(thread_id, token)
            if member.current_turn is not turn:
                raise SessionLeaseLost("member turn generation is stale")
            slot_id = member.resident_slot_id
            activation_processes = member.activation_processes
            observer_cursor = member.observer_cursor
            tool_mark_reader = member.tool_mark_reader
            window = member.window
            closing_census = window.census_seq + 1 if window is not None else 0
        # The closing census is taken while the window is still open and
        # before the observation is read, so the last cycle lands inside the
        # journal slice the observation is about to freeze. Sampling after
        # that would measure activity the slice does not contain.
        if window is not None:
            window.sampler.sample(window.handle, closing_census)
        # Close before the member reaper/permission normalizer below: those are
        # platform processes and must not be billed to the tool call whose
        # background children they clean up.
        observation = self._finish_guest_observation(group, observer_cursor)
        turn_resources = self._freeze_turn_window(thread_id, token, window, observation)
        telemetry = SharedTurnTelemetry(
            observation=observation,
            tool_marks=(
                tuple(tool_mark_reader.read() if tool_mark_reader is not None else ())
                + observation.tool_marks
            ),
            turn_resources=turn_resources,
        )
        boundary_error = self._enforce_activation_boundary(
            group, thread_id, slot_id=slot_id, activation_processes=activation_processes
        )
        detached = False
        lost_turn: _LostTurn | None = None
        try:
            with self._state.lock:
                _, member = self._state.leased_member(thread_id, token)
                if member.current_turn is not turn:
                    raise SessionLeaseLost("member turn generation is stale")
                # Detaching the turn hands its member lock to this call, which
                # releases it however the bookkeeping below ends.
                member.current_turn = None
                detached = True
                if member.lost_turn is not None and member.lost_turn.turn is turn:
                    lost_turn = member.lost_turn
                    member.lost_turn = None
                member.activation_processes = None
                member.observer_cursor = None
                member.tool_mark_reader = None
                member.window = None
                transport = member.transport
                self._record_boundary_state_locked(
                    member,
                    boundary_error,
                    thread_id=thread_id,
                    slot_id=slot_id,
                    activation_processes=activation_processes,
                )
        finally:
            if detached:
                member.turn_lock.release()
        if boundary_error is not None and transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if lost_turn is not None:
            # Nothing but this turn kept the activation: its process and
            # credential are gone. A lease that took on a newer process since
            # keeps it, and one that ended meanwhile has nothing left to end.
            with contextlib.suppress(SessionLeaseLost):
                self.abandon(thread_id, token, lost=True, residency=lost_turn.residency)
        if boundary_error is not None:
            raise SharedTurnBoundaryError(
                f"shared member activation boundary failed: {boundary_error}",
                telemetry,
            ) from boundary_error
        return telemetry

    def _freeze_turn_window(
        self,
        thread_id: str,
        token: str,
        window: MemberWindow | None,
        observation: GuestObservation,
    ) -> dict[str, Any] | None:
        """Close the member window, if this turn opened one, at its last report."""
        if window is None:
            return None
        with self._state.lock:
            self._state.leased_member(thread_id, token)
            window.report_seq += 1
            final_sequence = window.report_seq
        return window.freeze(observation, final_sequence)

    @staticmethod
    def _record_boundary_state_locked(
        member: _Member,
        boundary_error: Exception | None,
        *,
        thread_id: str,
        slot_id: int,
        activation_processes: tuple[tuple[int, int], ...] | None,
    ) -> None:
        """Set the member's resident state after its activation boundary.

        The caller holds the lock.
        """
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

    def _finish_guest_observation(
        self, group: _Group, observer_cursor: GuestObserverCursor | None
    ) -> GuestObservation:
        """Close the turn's guest process observation, or say why there is none."""
        observation = GuestObservation.unavailable("sandbox is not a Kata guest")
        if self._manager.backend.runtime == KATA_RUNTIME:
            finish_observation = getattr(self._manager, "finish_guest_observation", None)
            if finish_observation is None:
                observation = GuestObservation.unavailable(
                    "sandbox manager has no guest observer client",
                    source=GUEST_OBSERVER_SOURCE,
                )
            else:
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
        return observation

    def _enforce_activation_boundary(
        self,
        group: _Group,
        thread_id: str,
        *,
        slot_id: int,
        activation_processes: tuple[tuple[int, int], ...] | None,
    ) -> Exception | None:
        """Reap the member's leftover processes and normalize its files.

        Returns the failure instead of raising it: the caller must still detach
        the turn, then fail this settlement closed.
        """
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
            return exc
        return None

    def interrupt(self, thread_id: str, token: str) -> None:
        """Interrupt the exact member activation, leaving siblings untouched."""
        with self._state.lock:
            _, member = self._state.leased_member(thread_id, token)
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
        with self._state.lock:
            group = self._state.members.get(thread_id)
            member = group.members.get(thread_id) if group is not None else None
            turn = (
                member.current_turn if member is not None and member.lease_owner == owner else None
            )
            if turn is None:
                now = self._state.clock()
                held = [
                    entry
                    for entry in self._state.held_inputs.get(thread_id, [])
                    if now - entry.held_at < HELD_INPUT_TTL_S
                ]
                if all(entry.pending.message_id != message_id for entry in held):
                    held.append(_HeldInput(owner, PendingInput(message_id, text), now))
                self._state.held_inputs[thread_id] = held
                return InputOutcome(True)
        return turn.inject(text, message_id=message_id)

    def _question_turn(self, thread_id: str, owner: str) -> ResidentTurn[DialectRuntime] | None:
        """Find the owner's active turn while the lifecycle lock is held."""
        group = self._state.members.get(thread_id)
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
        with self._state.lock:
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
        with self._state.lock:
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
        with self._state.lock:
            _, member = self._state.leased_member(thread_id, token)
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
        with self._state.lock:
            group, member = self._state.member(thread_id)
            replay = self._replayed_settlement(
                member,
                token,
                job_id=job_id,
                context_status=context_status,
                native_session_id=native_session_id,
            )
            if replay is not None:
                return replay
            group, member = self._state.leased_member(thread_id, token)
            if member.current_turn is not None:
                raise SessionBusy("cannot settle a member while its turn is running")
            if member.transport is None or not member.transport.alive:
                self._log_unavailable_transport_locked(thread_id, member)
                raise SandboxError("cannot settle without a live resident transport")
            member.native_session_id = self._settled_session_id(
                member, member.transport, native_session_id
            )
            member.last_settled_job_id = job_id
            member.context_status = context_status
            # Preserve the observed native locator before gracefully retiring
            # a retry-affected CLI. Unexpected process death still fails the
            # live-transport check above, and no subsequent turn can enter
            # until EOF has flushed the native history.
            retiring = member.transport if member.transport.retire_after_turn else None
            retire_later: ResidentTransport[DialectRuntime] | None = None
            retire_key = f"retire:{thread_id}"
            if retiring is None:
                member.resident_state = "live"
            else:
                member.transport = None
                member.resident_state = "restarting"
                # Retiring waits for the CLI to flush, and for a Docker exec to
                # kill it if it does not exit, so it runs outside this lock.
                # Meanwhile the member lock keeps the member's next process
                # from resuming that history, and the group stays busy so no
                # capture freezes the CLI mid-flush. Whoever holds the member
                # lock already waits for this lock, so the CLI retires here.
                if member.turn_lock.acquire(blocking=False):
                    retire_later = retiring
                    group.operation_tokens[retire_key] = thread_id
                else:
                    self._retire(retiring, thread_id)
            member.lease_token = None
            member.lease_owner = None
            self._state.arm_idle_if_quiescent(group)
            descriptor = self._state.descriptor(group, member)
            member.settlement_receipt = (token, descriptor)
        if retire_later is not None:
            try:
                self._retire(retire_later, thread_id)
            finally:
                with self._state.lock:
                    group.operation_tokens.pop(retire_key, None)
                    self._state.arm_idle_if_quiescent(group)
                member.turn_lock.release()
        return descriptor

    @staticmethod
    def _log_unavailable_transport_locked(thread_id: str, member: _Member) -> None:
        """Log why the member has no live transport to settle with.

        The caller holds the lock.
        """
        diagnostic = "" if member.transport is None else member.transport.stderr_text()
        exit_code = None if member.transport is None else member.transport.exit_code
        logger.warning(
            "shared member resident transport is unavailable before settlement (exit_code=%s)",
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

    @staticmethod
    def _settled_session_id(
        member: _Member,
        transport: ResidentTransport[DialectRuntime],
        native_session_id: str | None,
    ) -> str:
        """Return the native session the live transport settles, refusing any mismatch."""
        observed = transport.native_session_id
        if native_session_id and observed and observed != native_session_id:
            raise SandboxError("resident transport reported a different native session id")
        settled_session_id = observed or native_session_id
        if not settled_session_id:
            raise SandboxError("resident transport did not report a native session id")
        if member.native_session_id and settled_session_id != member.native_session_id:
            raise SandboxError("resident transport settled a different native session")
        return settled_session_id

    @staticmethod
    def _retire(transport: ResidentTransport[DialectRuntime], thread_id: str) -> None:
        """Close a settled member's CLI; the settlement stands whatever the close says.

        The settlement has recorded the CLI's native locator already. A close
        that cannot confirm the exit leaves the process to the member's next
        launch: Claude Code, the one CLI that retires, reaps its slot first.
        """
        try:
            transport.close()
        except Exception as exc:
            logger.warning(
                "retiring shared member CLI did not confirm its exit",
                extra={
                    "event": "shared_member_retire_close_failed",
                    "thread_id": thread_id,
                    "error_type": _safe_error_type(exc),
                },
            )

    def abandon(
        self,
        thread_id: str,
        token: str,
        *,
        lost: bool,
        residency: tuple[str, int] | None = None,
    ) -> SharedSessionDescriptor:
        """Release one failed member; restart/lose it without killing siblings.

        ``residency`` releases the lease only while it still names that
        residency, checked under the same lock as the lease itself: a lease
        that has taken on a newer process meanwhile keeps it.
        """
        with self._state.lock:
            group, member = self._state.leased_member(thread_id, token)
            if residency is not None and member.lease_residency != residency:
                return self._state.descriptor(group, member)
            turn = member.current_turn
            # This teardown ends the activation itself, so the turn it closes
            # below must not end it first.
            member.lost_turn = None
        if turn is not None:
            with contextlib.suppress(Exception):
                turn.interrupt()
            with contextlib.suppress(Exception):
                self.complete_turn(thread_id, token, turn)
        with self._state.lock:
            group, member = self._state.leased_member(thread_id, token)
            if residency is not None and member.lease_residency != residency:
                return self._state.descriptor(group, member)
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
            member.window = None
            member.lease_token = None
            member.lease_owner = None
            # Teardown owns the member now, so a stale acknowledgement must not
            # replay against whatever state the restart leaves behind.
            member.settlement_receipt = None
            member.context_status = "lost" if lost else member.context_status
            member.resident_state = (
                "lost" if lost else "restarting" if member.native_session_id else "stopped"
            )
            self._state.arm_idle_if_quiescent(group)
            descriptor = self._state.descriptor(group, member)
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
        """Close only the process whose renewable grant fence was rejected.

        The activation that took that process on ends with it, as
        ``abandon(lost=True)`` would end it: its CLI and credential are gone,
        and when the control plane reaped its attempt nothing else is left to
        settle or abandon it, so its lease would keep the group busy for good.
        While its turn still runs, the turn stays with its stream, which still
        answers for what the turn did; the activation ends once that turn is
        closed. A runner that hung still holding the stream may never read it
        to the end, so the sweep closes a turn still open ``LOST_TURN_GRACE_S``
        after the loss. An activation that is replacing the process under a
        newer residency has not taken it on, and keeps its lease, including
        one that starts the replacement after the old process is closed here.
        """
        with self._state.lock:
            group, member = self._state.member(thread_id)
            if member.residency_generation != generation:
                return self._state.descriptor(group, member)
            turn = member.current_turn
            transport = member.transport
            lease_token = member.lease_token
            # The lease whose activation took this process on, if any.
            taken_by = (
                lease_token
                if lease_token is not None and member.lease_residency == (lease_token, generation)
                else None
            )
            ended_lease = taken_by if turn is None else None
            if taken_by is not None and turn is not None:
                member.lost_turn = _LostTurn(turn, (taken_by, generation), self._state.clock())
            # An active observation belongs to the turn, not to the resident
            # transport.  Keep it until complete_turn/abort_turn closes the
            # turn so the events already captured before residency loss are
            # still returned to the caller.  Drain only an orphaned cursor
            # when there is no turn left to own it.
            orphaned_observer_cursor = None
            orphaned_window = None
            if turn is None:
                orphaned_observer_cursor = member.observer_cursor
                member.observer_cursor = None
                member.tool_mark_reader = None
                # The window is the cursor's counterpart: a registration the
                # observer still holds. With no turn left to close it, this is
                # the only place that can, and leaving it open would keep
                # measuring a member that is no longer running a turn.
                orphaned_window = member.window
                member.window = None
            member.transport = None
            member.residency_generation = 0
            member.resident_state = "restarting" if member.native_session_id else "stopped"
        if turn is not None:
            with contextlib.suppress(Exception):
                turn.interrupt()
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if orphaned_window is not None:
            orphaned_window.close()
        if orphaned_observer_cursor is not None:
            with contextlib.suppress(Exception):
                self._manager.finish_guest_observation(group.spec, orphaned_observer_cursor)
        if ended_lease is not None:
            # A settlement or abandon that released the lease meanwhile has
            # ended the activation already. One that rotated it to a newer
            # residency meanwhile has a process this loss does not name.
            with contextlib.suppress(SessionLeaseLost):
                return self.abandon(
                    thread_id, ended_lease, lost=True, residency=(ended_lease, generation)
                )
        with self._state.lock:
            return self._state.descriptor(group, member)

    def close_lost_turns(self) -> None:
        """End each activation whose lost turn its stream has not closed in time.

        The loss already interrupted the turn and closed its process. Ending
        the activation closes the turn's boundary as its stream would have,
        and still releases the turn and its lease if that boundary fails.
        """
        now = self._state.clock()
        due: builtins.list[tuple[str, tuple[str, int]]] = []
        with self._state.lock:
            for group in self._state.groups.values():
                for member in group.members.values():
                    lost = member.lost_turn
                    if lost is None:
                        continue
                    if (
                        member.current_turn is not lost.turn
                        or member.lease_token != lost.residency[0]
                    ):
                        # Closed or torn down some other way.
                        member.lost_turn = None
                    elif now - lost.lost_at >= LOST_TURN_GRACE_S:
                        due.append((member.thread_id, lost.residency))
        for thread_id, residency in due:
            try:
                self.abandon(thread_id, residency[0], lost=True, residency=residency)
            except SandboxError:
                # Its stream closed the turn meanwhile, which ended the
                # activation, or the activation ended some other way.
                continue
            except Exception as exc:
                logger.error(
                    "lost shared member turn could not be closed",
                    extra={
                        "event": "shared_member_lost_turn_close_failed",
                        "thread_id": thread_id,
                        "error_type": _safe_error_type(exc),
                    },
                )

    def ensure_transport(
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
        with self._state.lock:
            current_group, current_member = self._state.member(member.thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("member identity changed before process launch")
            if lease_token is not None and member.lease_token != lease_token:
                raise SessionLeaseLost("shared member activation lease is stale")
            group.lifecycle.admit(Operation.MUTATION)
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
                if lease_token is not None:
                    member.lease_residency = (lease_token, member.residency_generation)
                return "reuse"
            if current is not None and current.alive and not launch.credential:
                # A probe from a new turn intentionally omits credentials so a
                # HOT hit never rotates the residency grant. Preserve the
                # existing process until the caller explicitly obtains a
                # replacement grant. The message names what differs, never values.
                raise SandboxError(probe_refusal_message(current, launch, same_residency))
            member.transport = None
            member.resident_state = "restarting" if member.native_session_id else "starting"
        if current is not None:
            with contextlib.suppress(Exception):
                current.close()
        with substage(observer, "cli_spawn"):
            transport = self._transport_factory.open(group.spec, launch)
        self._refuse_unfenced_process(transport, launch)
        launch_invalid = False
        with self._state.lock:
            rechecked_group = self._state.members.get(member.thread_id)
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
                self._install_transport_locked(member, transport, launch, lease_token)
        if launch_invalid:
            with contextlib.suppress(Exception):
                transport.close()
            raise SessionLeaseLost("shared sandbox lifecycle changed during process launch")
        # A freshly opened process is a native resume when there was a session
        # to resume, and a new CLI otherwise. It is never a reuse: the HOT
        # process returned above.
        return "resume" if launch.native_session_id else "new"

    @staticmethod
    def _refuse_unfenced_process(
        transport: ResidentTransport[DialectRuntime], launch: ResidentLaunch
    ) -> None:
        """Close and refuse a new process that resumed another session or has no fence."""
        observed = transport.native_session_id
        if launch.native_session_id and observed and observed != launch.native_session_id:
            with contextlib.suppress(Exception):
                transport.close()
            raise SandboxError("resident transport resumed a different native session")
        if launch.residency_generation <= 0:
            with contextlib.suppress(Exception):
                transport.close()
            raise SandboxError("a new resident process needs a residency generation fence")

    @staticmethod
    def _install_transport_locked(
        member: _Member,
        transport: ResidentTransport[DialectRuntime],
        launch: ResidentLaunch,
        lease_token: str | None,
    ) -> None:
        """Make a freshly opened process the member's live transport.

        The caller holds the lock and has rechecked the member's lease.
        """
        member.transport = transport
        member.process_generation += 1
        member.residency_generation = launch.residency_generation
        if lease_token is not None:
            member.lease_residency = (lease_token, launch.residency_generation)
        member.native_session_id = transport.native_session_id or member.native_session_id
        # HOT means this exact process accepted another turn. Any newly opened
        # process is a native resume (or the first NEW process), even when
        # acquire observed the predecessor alive before a model/MCP/residency
        # compatibility check replaced it.
        member.context_status = "restored" if launch.native_session_id else "new"
        member.resident_state = "live"

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
            environment_snapshot_id=(
                group.inspection.labels.get(SNAPSHOT_LABEL, "").partition(":")[2] or None
            ),
            environment_snapshot_failure=group.environment_snapshot_failure,
        )


__all__ = [
    "HELD_INPUT_TTL_S",
    "LOST_TURN_GRACE_S",
    "MemberSessions",
    "SharedTurnBoundaryError",
    "SharedTurnTelemetry",
]
