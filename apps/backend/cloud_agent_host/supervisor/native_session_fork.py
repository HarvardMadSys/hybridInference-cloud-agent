"""Fork a settled member's native session into a new member that is live before it returns.

The Fork's activity token keeps the group from stopping, and only the source
member's turn lock is held while its process is prepared and forked: sibling
turns and terminals continue meanwhile.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxSpec,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.drivers import DialectRuntime
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransport,
    ResidentTransportFactory,
)
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.supervisor.group_lifecycle import Operation
from cloud_agent_host.supervisor.shared_session_state import (
    SharedSessionState,
    _Group,
    _Member,
)

if TYPE_CHECKING:
    from cloud_agent_host.supervisor.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.supervisor.member_sessions import MemberSessions

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


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


NativeForker = Callable[
    [ChatSandboxSpec, ResidentTransport[DialectRuntime], str, ResidentLaunch, int],
    str,
]


class ForkService:
    """Run native session forks inside one group."""

    def __init__(
        self,
        state: SharedSessionState,
        *,
        lifecycle: GroupLifecycleController,
        sessions: MemberSessions,
        transport_factory: ResidentTransportFactory,
        native_forker: NativeForker,
    ) -> None:
        self._state = state
        self._lifecycle = lifecycle
        self._sessions = sessions
        self._transport_factory = transport_factory
        self._native_forker = native_forker

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
        """Fork immediately, then prove the child live before returning.

        ``start_seq`` is the fence the control plane took for the Fork.
        """
        if self._native_forker is None:
            raise SandboxError("native session fork is not configured on this host")
        with self._state.lock:
            group, source = self._state.member(source_thread_id)
        self._lifecycle.recover_start_archive_completion(group)
        with self._state.lock:
            current_group, current_source = self._state.member(source_thread_id)
            if current_group is not group or current_source is not source:
                raise SessionLeaseLost("source membership changed before fork admission")
            group.lifecycle.admit(Operation.MUTATION)
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
                        start_seq=start_seq,
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
                with self._state.lock:
                    group.operation_tokens.pop(activity_token, None)
                    self._state.arm_idle_if_quiescent(group)

    def _fork_native_locked_source(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        self,
        group: _Group,
        source: _Member,
        *,
        start_seq: int | None,
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
        self._lifecycle.ensure_running(group, start_seq=start_seq)
        stale_transport: ResidentTransport[DialectRuntime] | None = None
        with self._state.lock:
            group.lifecycle.admit(Operation.MUTATION)
            # The Fork's activity token keeps the group from stopping.
            self._state.join_run_locked(group, start_seq)
            fork_lifecycle_generation = group.life.generation
            current_group, current_source = self._state.member(source_thread_id)
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
            self._sessions.ensure_transport(group, source, source_launch)

        with self._state.lock:
            if source.transport is None or not source.transport.alive:
                raise SandboxError("source native transport is not live")
            existing_target: _Member | None = None
            if target_thread_id in self._state.members:
                target_group, target = self._state.member(target_thread_id)
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
                self._sessions.ensure_transport(group, existing_target, child_launch)
            finally:
                existing_target.turn_lock.release()
            with self._state.lock:
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
            with self._state.lock:
                current_group, current_source = self._state.member(source_thread_id)
                if current_group is not group or current_source is not source:
                    raise SessionLeaseLost("source membership changed after native fork")
                if source.transport is not source_transport:
                    raise SessionLeaseLost("source transport changed after native fork")
                source.transport = None
                source.resident_state = "restarting"
            with contextlib.suppress(Exception):
                source_transport.close()
            self._sessions.ensure_transport(group, source, source_launch)
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

        with self._state.lock:
            if group.life.stop is not None or group.life.generation != fork_lifecycle_generation:
                child.transport.close()
                raise SessionLeaseLost("shared sandbox lifecycle changed during native fork")
            if target_thread_id in self._state.members:
                child.transport.close()
                raise SandboxError("fork target raced with another membership")
            group.members[target_thread_id] = child
            self._state.members[target_thread_id] = group
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


__all__ = [
    "ForkService",
    "NativeForker",
    "NativeSessionForkResult",
]
