"""Prepare a group's bound environment while the workspace's other writers are held off.

Preparation holds the group's exclusive-workspace barrier (``snapshotting``)
and freezes the members' writers around any script it runs. A boot whose
receipt is already ready for the exact running container is answered without
either (:func:`~cloud_agent_host.supervisor.environment_ready_boot.ready_boot`).
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxInspection,
    RetainedChatLost,
)
from cloud_agent_host.container.retained_backend import RetainedChatBackend
from cloud_agent_host.environment_preparation import prepared_in, runs_no_script
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.supervisor.environment_ready_boot import ready_boot
from cloud_agent_host.supervisor.group_lifecycle import Operation
from cloud_agent_host.supervisor.group_lifecycle_state import ExclusiveHolder
from cloud_agent_host.supervisor.shared_session_state import (
    _SAME_RUN,
    SharedSessionState,
    _Group,
    _Member,
    _SameRun,
)

if TYPE_CHECKING:
    from cloud_agent_host.supervisor.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.supervisor.group_lifecycle_state import Hold

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


class GroupEnvironmentPreparation:
    """Serialize one group's environment preparation with its writers."""

    def __init__(self, state: SharedSessionState, *, lifecycle: GroupLifecycleController) -> None:
        self._state = state
        self._manager = state.manager
        self._lifecycle = lifecycle

    def prepare_environment(
        self,
        thread_id: str,
        environment: Any,
        preparer: Any,
        *,
        activation_owner: str | None = None,
        start_seq: int | _SameRun | None = _SAME_RUN,
    ) -> dict[str, Any]:
        """Serialize preparation with writers, preserving this activation's lease.

        As in :meth:`status`, Docker and the preparation receipt are read outside
        the metadata lock, which every other group's calls would otherwise wait
        behind. An observation that a lifecycle call superseded while it was in
        flight never replaces that call's own.

        ``start_seq`` is the fence of an owner's request for the preparation.
        A turn's own preparation runs under the fence its activation took.
        """
        if start_seq is None and activation_owner is not None:
            start_seq = _SAME_RUN
        ready = ready_boot(self._state, thread_id, environment, preparer, activation_owner)
        if ready is not None:
            return ready
        with self._state.lock:
            group, _ = self._state.member(thread_id)
        self._lifecycle.ensure_running(group, start_seq=start_seq)
        with self._state.lock:
            group, member = self._state.member(thread_id)
            observed = group.inspection
            lifecycle_generation = group.life.generation
        backend = RetainedChatBackend(self._manager, group.spec)
        current = self._manager.inspect(group.spec.container_name)
        receipt = preparer.status(backend, environment.binding_id)
        with self._state.lock:
            current_group, current_member = self._state.member(thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("shared sandbox membership changed during observation")
            self._accept_observation_locked(group, observed, lifecycle_generation, current)
            if receipt.get("ready") and prepared_in(receipt, group.inspection):
                return receipt
            hold, cancelled, generation, expected, slots = self._reserve_preparation_locked(
                group, activation_owner=activation_owner, start_seq=start_seq
            )
        monitor_stop = threading.Event()
        monitor_thread = self._start_lease_fence(
            group,
            member,
            generation=generation,
            activation_owner=activation_owner,
            cancelled=cancelled,
            monitor_stop=monitor_stop,
        )
        manifest = None
        try:
            # Writers are frozen only around a script: preparing a boot that
            # runs none only makes the tool home and records the start.
            manifest = (
                None
                if runs_no_script(receipt, environment, expected)
                else self._manager.freeze_shared_writers(
                    group.spec,
                    slot_ids=slots,
                    token=secrets.token_hex(16),
                    expected_container_id=expected,
                )
            )
            result = preparer.prepare(backend, environment, cancel=cancelled)
            self._state.require_current_inspection(group)
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
                with self._state.lock:
                    group.life.end_exclusive_locked(hold)
                    group.environment_cancel = None
                    group.environment_owner = None
                    self._state.arm_idle_if_quiescent(group)

    def _accept_observation_locked(
        self,
        group: _Group,
        observed: ChatSandboxInspection,
        lifecycle_generation: int,
        current: ChatSandboxInspection | None,
    ) -> None:
        """Accept an unlocked Docker observation no lifecycle call superseded.

        The caller holds the lock.
        """
        if group.inspection == observed and group.life.generation == lifecycle_generation:
            if current is None:
                self._state.mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            self._state.accept_inspection_locked(group, current)
        elif group.life.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")

    def _reserve_preparation_locked(
        self,
        group: _Group,
        *,
        activation_owner: str | None,
        start_seq: int | _SameRun | None,
    ) -> tuple[Hold, threading.Event, int, str, list[int]]:
        """Take the group's exclusive-workspace barrier for this preparation.

        The caller holds the lock. Returns the barrier's hold, the preparation's
        cancel event, the lifecycle generation and container it is fenced to,
        and the member slots whose writers it may freeze.
        """
        group.lifecycle.admit(Operation.MUTATION)
        if group.operation_tokens or any(
            m.current_turn is not None
            or (m.lease_token is not None and m.lease_token != activation_owner)
            for m in group.members.values()
        ):
            raise SessionBusy("Environment preparation waits for active workspace operations")
        self._state.join_run_locked(group, start_seq)
        cancelled = threading.Event()
        group.environment_cancel = cancelled
        group.environment_owner = activation_owner
        hold = group.life.begin_exclusive_locked(ExclusiveHolder.ENVIRONMENT)
        generation = group.life.generation
        expected = group.inspection.container_id
        slots = [m.resident_slot_id for m in group.members.values()]
        return hold, cancelled, generation, expected, slots

    def _start_lease_fence(
        self,
        group: _Group,
        member: _Member,
        *,
        generation: int,
        activation_owner: str | None,
        cancelled: threading.Event,
        monitor_stop: threading.Event,
    ) -> threading.Thread:
        """Start the thread that cancels preparation once its lease or lifecycle moves on."""

        def monitor() -> None:
            while not monitor_stop.wait(0.5):
                with self._state.lock:
                    if group.life.generation != generation or (
                        activation_owner is not None and member.lease_token != activation_owner
                    ):
                        cancelled.set()
                        return

        monitor_thread = threading.Thread(
            target=monitor, daemon=True, name="environment-lease-fence"
        )
        monitor_thread.start()
        return monitor_thread

    def cancel_environment(self, thread_id: str, activation_owner: str) -> None:
        """Only the preparing activation may revoke its long-running setup."""
        with self._state.lock:
            group, _ = self._state.member(thread_id)
            if group.environment_owner == activation_owner and group.environment_cancel:
                group.environment_cancel.set()


__all__ = [
    "GroupEnvironmentPreparation",
]
