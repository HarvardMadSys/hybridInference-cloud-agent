"""Capture the pre-prompt workspace of one fresh activation, every other writer excluded.

The capture holds the group's exclusive-workspace barrier (``snapshotting``)
and freezes the members' writers while Docker copies the workspace. A failed
capture whose immutable initial object may exist fail-stops the group through
the lifecycle controller and keeps the writers frozen.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.archive.archive_failure_diagnostics import (
    _archive_failure_diagnostic,
    _record_archive_failure_diagnostics,
    _safe_error_type,
)
from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxSpec,
    RetainedChatBackend,
    RetainedChatLost,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.group_lifecycle import Operation
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.shared_session_state import (
        SharedMemberManifest,
        SharedSessionState,
        _Group,
        _Member,
    )

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


#: The pre-prompt archive's bound on waiting for a concurrent bridge check or
#: workspace request (the chat page reads the workspace while a chat prepares).
INITIAL_ARCHIVE_WAIT_S = 30.0


class InitialArchiveCapture:
    """Capture one activation's pre-prompt workspace archive."""

    def __init__(self, state: SharedSessionState, *, lifecycle: GroupLifecycleController) -> None:
        self._state = state
        self._manager = state.manager
        self._lifecycle = lifecycle
        #: How long the pre-prompt archive waits for a concurrent bridge check
        #: or workspace request to finish before it reports the group busy.
        self.initial_archive_wait_s = INITIAL_ARCHIVE_WAIT_S

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
                group.lifecycle.admit(Operation.MUTATION)
            finally:
                group.snapshotting = True
        else:
            group.lifecycle.admit(Operation.MUTATION)
        if expected_spec is not None and group.spec != expected_spec:
            raise SandboxError("shared sandbox immutable identity/configuration changed")
        if expected_container_id is not None and group.inspection.container_id != (
            expected_container_id
        ):
            raise RetainedChatLost("shared sandbox container identity changed")
        if group.container_lost:
            raise RetainedChatLost("shared sandbox container identity was lost")
        if member.current_turn is not None or member.transport is not None:
            raise SessionBusy("initial archive must precede resident process creation")
        if any(
            peer is not member and (peer.lease_token is not None or peer.current_turn is not None)
            for peer in group.members.values()
        ):
            raise SessionBusy("a sibling member is active during initial archive capture")

    def capture_initial_archive(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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

        This boundary deliberately permits the addressed activation lease: the runner has completed checkout/setup
        but has not created a resident CLI process or admitted its prompt.
        Every other writer remains excluded, and setup descendants are frozen
        while Docker copies the workspace.
        """
        with self._state.lock:
            group, member = self._state.leased_member(thread_id, token)
            deadline = time.monotonic() + self.initial_archive_wait_s
            # A workspace read revalidates the bridge under a temporary start
            # fence, and the chat page reads the workspace while a new chat
            # prepares. Wait for that check as a prompt does, not fail on it.
            self._state.wait_while_locked(
                lambda: (
                    "shared workspace start is in progress"
                    if group.starting
                    else "shared sandbox lifecycle check is in progress"
                    if group.lifecycle_check is not None
                    else None
                ),
                deadline=deadline,
            )
            group, member = self._state.leased_member(thread_id, token)
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
                # A request admitted before the barrier may still begin its
                # bridge revalidation under it (``_start_group`` does not read
                # the barrier), so its start fence is waited out here too.
                self._state.wait_while_locked(
                    lambda: (
                        "shared workspace start is in progress"
                        if group.starting
                        else "shared sandbox lifecycle check is in progress"
                        if group.lifecycle_check is not None
                        else "shared workspace still has active mutations"
                        if group.operation_tokens or group.terminal_tokens
                        else None
                    ),
                    deadline=deadline,
                )
                # The wait released the lock; the lease and group may have moved.
                leased_group, leased_member = self._state.leased_member(thread_id, token)
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
                self._state.arm_idle_if_quiescent(group)
                self._state.condition.notify_all()
                raise
            observed = group.inspection
            lifecycle_generation = group.lifecycle_generation
            authoritative_spec = group.spec
            authoritative_members = self._state.archive_members_locked(group)
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
            with self._state.lock:
                if group.inspection == observed:
                    group.inspection = current
        except BaseException:
            with self._state.lock:
                group.snapshotting = False
                self._state.arm_idle_if_quiescent(group)
                self._state.condition.notify_all()
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
                with self._state.lock:
                    group.initial_archive_preservation_pending = True
                try:
                    stopped = self._lifecycle.stop_if_idle(
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
                    with self._state.condition:
                        while group.stopping:
                            self._state.condition.wait(timeout=0.5)
                    try:
                        current = self._state.require_current_inspection(group)
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
                self._state.require_current_inspection(group)
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
                self._state.require_current_inspection(group)
            except BaseException as exc:
                if identity_error is None:
                    identity_error = exc
        with self._state.lock:
            current_group = self._state.members.get(thread_id)
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
                self._state.arm_idle_if_quiescent(group)
            elif preserve_frozen_writers:
                group.initial_archive_preservation_pending = True
            self._state.condition.notify_all()
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


__all__ = [
    "INITIAL_ARCHIVE_WAIT_S",
    "InitialArchiveCapture",
]
