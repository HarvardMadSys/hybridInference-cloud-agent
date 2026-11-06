"""Start, stop, network fail-stop and archive deletion of one shared sandbox group.

A start completes the group's durable start fence; a stop reserves the group's
lifecycle, inspects and classifies its exact container, flushes the members'
native history and stops the container before its final archive; an archive
deletion holds the same reservation across its Docker checks. Each runs its
Docker, marker and archive I/O without the metadata lock and revalidates the
reserved group before it accepts a result. The network guard's fail-stop and
the shutdown stop of every group go through the same stop. The final-archive
steps here are also how reconciliation finishes an orphan's archive.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.archive.archive_failure_diagnostics import (
    _archive_failure_diagnostic,
    logged_stopped_archive_failure,
)
from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatFailStopped,
    RetainedChatLost,
    SandboxActionFacts,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.network.guarded_workloads import (
    GuardedWorkload,
    exact_stop_unguarded,
    host_rule_failure,
    merge_guarded_inventories,
    retry_unresolved_stops,
    running_guarded_workloads,
    workloads_with_known,
)
from cloud_agent_host.network.idle_host_rules import IdleHostRules
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
    StaleStartFence,
)
from cloud_agent_host.supervisor.group_lifecycle import Operation
from cloud_agent_host.supervisor.group_lifecycle_state import Hold, StartOutcome, StopHolder
from cloud_agent_host.supervisor.shared_admission_proof import ensure_running
from cloud_agent_host.supervisor.shared_session_state import (
    _SAME_RUN,
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionState,
    _Group,
    _LifecycleCheck,
    _Member,
    _SameRun,
)
from cloud_agent_host.upload_import import forget_deleted_container_uploads

if TYPE_CHECKING:
    import builtins

    from cloud_agent_shared.operation_profiling import (
        StageRecorder,
    )


# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


class GroupLifecycleController:
    """Own the start, stop and final archive of each group, one transition at a time."""

    def __init__(self, state: SharedSessionState) -> None:
        self._state = state
        self._manager = state.manager
        self._archive = state.archive
        self._archives_finals = state.archives_finals
        self._idle_host_rules = IdleHostRules(state.manager)

    @property
    def state(self) -> SharedSessionState:
        """The ownership this controller's admissions and proofs run under."""
        return self._state

    def prepare_markerless_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
    ) -> bool:
        """Ask the dynamic policy owner to authorize markerless cleanup.

        A static archive service has no such authority and therefore keeps the
        conservative quarantine behavior.  The dynamic controller writes the
        disabled-policy marker atomically before returning ``True``.
        """
        if not self._archives_finals:
            return False
        try:
            return self._archive.markerless_final(spec, inspection)
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

    def stop_all_for_shutdown(self, *, include_running: bool = True) -> None:
        """Cleanly stop and archive every running group during sandbox manager shutdown.

        The sandbox manager calls this only when final archival is configured, after
        :meth:`quiesce` has stopped background admission but before
        :meth:`close` detaches native transports. A failed archive remains
        pending and is reported after the other groups have also had a chance
        to stop.
        """
        with self._state.lock:
            candidates = [
                (next(iter(group.members)), group.spec.generation, group)
                for group in self._state.groups.values()
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
            hold: Hold | None = None
            with self._state.lock:
                retry_pending = group.stop_archive_pending
                blocked = group.stop_archive_blocked
                if retry_pending:
                    if (refused := group.lifecycle.refusal(Operation.TAKE_STOP)) is not None:
                        failures.append(refused)
                        continue
                    hold = group.life.begin_stop_locked(StopHolder.SHUTDOWN_RETRY)
            if hold is not None:
                try:
                    if blocked:
                        self.stop_blocked_group(group)
                        with self._state.lock:
                            still_blocked = group.stop_archive_blocked
                        if still_blocked:
                            failures.append(
                                SandboxError(
                                    "shared sandbox final archive skip could not be persisted"
                                )
                            )
                    else:
                        self.retry_stopped_archive(group)
                except Exception as exc:
                    failures.append(exc)
                finally:
                    with self._state.lock:
                        group.life.end_stop_locked(hold)
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
        with self._state.lock:
            return sum(
                group.inspection.running
                or group.stop_archive_pending
                or group.orphan_finalization_pending
                or group.start_archive_completion_pending
                for group in self._state.groups.values()
            )

    def has_pending_archive_work(self) -> bool:
        """Whether shutdown must preserve or finish an older archive lifecycle."""
        with self._state.lock:
            return any(
                group.stop_archive_pending
                or group.stop_archive_blocked
                or group.orphan_finalization_pending
                or group.start_archive_completion_pending
                for group in self._state.groups.values()
            )

    def read_final_marker(
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
    def marker_result(payload: bytes | None, error: BaseException | None) -> bytes | None:
        """Apply an out-of-lock marker read inside its original failure handling."""
        if error is not None:
            raise error
        return payload

    def persist_stopped_archive(
        self,
        group: _Group,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> ChatSandboxInspection:
        """Persist one clean stop, then release its durable retry marker."""
        if inspection.running:
            raise SandboxError("refusing to archive a running shared sandbox as stopped")
        with self._state.lock:
            self._state.accept_inspection_locked(group, inspection)
            if not self._archives_finals:
                group.life.settle_archive_locked()
                return inspection
            group.life.owe_final_locked(members)
            start_seq = None if group.run_unfenced else group.start_seq  # this run's own
        with logged_stopped_archive_failure("final_archive", group.spec, inspection.container_id):
            self._archive.stopped(group.spec, inspection, members, start_seq=start_seq)
        # The owned bridge is the durable marker that a stopped container has
        # not completed its final archive. Release it only after the upload
        # returned successfully, using the same exact-container fence as the
        # original stop. A cleanup failure leaves ``stop_archive_pending`` and
        # the bridge intact so this operation is retried rather than reported
        # as a clean stop.
        with logged_stopped_archive_failure("network_release", group.spec, inspection.container_id):
            released = self._manager.stop(
                group.spec, expected_container_id=inspection.container_id, release_networks=True
            )
        with self._state.lock:
            self._state.accept_inspection_locked(group, released)
            group.life.settle_archive_locked()
        return released

    def retry_stopped_archive(self, group: _Group, *, persist: bool = True) -> None:
        """Finish a prepared stop, optionally deferring archive persistence."""
        with self._state.lock:
            members = group.stop_archive_members
            pending = group.stop_archive_pending
        if not pending or members is None:
            return
        inspection = self._state.require_current_inspection(group)
        if inspection.running:
            try:
                self._archive.prepare_final(group.spec, inspection)
            except Exception:
                if not persist:
                    # Network fail-stop phase one cannot leave a group
                    # running merely because its prepared marker is no
                    # longer usable. Skip this lifecycle's final archive:
                    # this group must never upload unproven history.
                    with self._state.lock:
                        group.life.owe_final_locked(members, blocked=True)
                    self.stop_blocked_group(group, inspection)
                    with self._state.lock:
                        if not group.stop_archive_blocked:
                            return
                raise
            inspection = self._manager.stop(
                group.spec,
                expected_container_id=inspection.container_id,
                release_networks=not self._archives_finals,
            )
            with self._state.lock:
                self._state.accept_inspection_locked(group, inspection)
        with self._state.lock:
            for member in group.members.values():
                member.resident_state = (
                    "lost"
                    if member.resident_state == "lost" or member.context_status == "lost"
                    else "stopped"
                )
            members = self._state.archive_members_locked(group)
            group.life.refresh_final_members_locked(members)
        if not persist:
            return
        self.persist_stopped_archive(group, inspection, members)

    def stop_blocked_group(
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
        current = inspection or self._state.require_current_inspection(group)
        marker_persisted = False
        try:
            marker_persisted = self._archive.invalidate_final(group.spec, current)
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
                release_networks=not self._archives_finals,
            )
            with self._state.lock:
                self._state.accept_inspection_locked(group, current)
        if not marker_persisted:
            try:
                marker_persisted = self._archive.invalidate_final(group.spec, current)
            except Exception:
                logger.exception(
                    "stopped sandbox block marker retry failed",
                    extra={
                        "event": "sandbox_archive_stopped_block_marker_retry_failed",
                        "sandbox_id": group.spec.sandbox_id,
                        "generation": group.spec.generation,
                    },
                )
        if not marker_persisted and self._archives_finals:
            return current
        with self._state.lock:
            members = group.stop_archive_members or self._state.archive_members_locked(group)
        # The skipped marker makes the final hook a no-op, so this only
        # releases the bridge and clears the fences.
        return self.persist_stopped_archive(group, current, members)

    def start_group(
        self,
        group: _Group,
        *,
        start_seq: int | _SameRun | None = _SAME_RUN,
        facts: SandboxActionFacts | None = None,
        observer: StageRecorder | None = None,
    ) -> tuple[ChatSandboxInspection, bool]:
        """Start a group and archive a guest-network fail-stop exactly once.

        ``start_seq`` is the fence of the request the start is for. When the
        container is found stopped, the run this starts is labelled with it,
        and a fence already sealed is refused (``GroupLifecycleState.begin_run_locked``).
        A running container is reused as it is: the request joins its run
        once it is admitted (:meth:`SharedSessionState.join_run_locked`). The flag says whether
        ``start`` proved the network here; a retried archive completion did not.
        """
        with self._state.lock:
            self._state.wait_for_start_locked(group, retry_failed=True)
            hold, members, expected_container_id = self._admit_start_locked(group)
        start_fence_prepared = False
        start_completed = False
        refused = False

        def prepare_stopped_start(current: ChatSandboxInspection) -> None:
            nonlocal start_fence_prepared
            # Docker has just proved this exact container stopped, and starts
            # it next: this is where a new run begins.
            if start_seq is not _SAME_RUN:
                with self._state.lock:
                    group.life.begin_run_locked(start_seq)
            if not self._archive.prepare_start(group.spec, current):
                return
            with self._state.lock:
                self._state.accept_inspection_locked(group, current)
                group.life.owe_start_completion_locked()
                start_fence_prepared = True

        try:
            retried = self._retry_start_completion(group)
            if retried is not None:
                start_completed = True
                return retried, False
            inspection = self._manager.start(
                group.spec,
                facts=facts,
                observer=observer,
                expected_container_id=expected_container_id,
                defer_network_release_on_fail_stop=self._archives_finals,
                before_stopped_start=prepare_stopped_start,
            )
        except StaleStartFence:
            # Refused before Docker was asked: nothing about the start failed.
            refused = True
            raise
        except RetainedChatLost:
            with self._state.lock:
                self._state.mark_container_lost_locked(group)
            raise
        except RetainedChatFailStopped as exc:
            self._archive_fail_stopped_start(group, exc.inspection, members)
            raise
        except BaseException:
            if not start_fence_prepared:
                raise
            self._fail_stop_uncertain_start(group, members, expected_container_id)
            raise
        else:
            self._complete_start(group, inspection, start_fence_prepared=start_fence_prepared)
            start_completed = True
            return inspection, True
        finally:
            with self._state.lock:
                group.life.end_start_locked(
                    hold,
                    StartOutcome.REFUSED
                    if refused
                    else StartOutcome.COMPLETED
                    if start_completed
                    else StartOutcome.FAILED,
                )
                group.lifecycle.record_combination(site="end_start")

    def _admit_start_locked(
        self, group: _Group
    ) -> tuple[Hold, tuple[SharedMemberManifest, ...], str]:
        """Admit a start and mark the group starting.

        The caller holds the lock. Returns the start's hold, the final-archive
        membership and the container the start is for.
        """
        group.lifecycle.admit(Operation.START)
        hold = group.life.begin_start_locked()
        members = self._state.archive_members_locked(group)
        return hold, members, group.inspection.container_id

    def _retry_start_completion(self, group: _Group) -> ChatSandboxInspection | None:
        """Finish a running group's pending start archive completion, if it has one.

        Returns the running inspection it completed, or None when the group
        needs an ordinary start.
        """
        with self._state.lock:
            completion_pending = group.start_archive_completion_pending
        if completion_pending:
            current = self._state.require_current_inspection(group)
            if current.running:
                # ``start_archive_completion_pending`` is set only after
                # ``manager.start`` returned a fully network-verified running
                # container and the durable marker completion hook failed.
                # Retry that idempotent completion in place. A cold stop here
                # would introduce a markerless crash window after the
                # completion hook had already cleared the marker.
                self._archive.started(group.spec, current)
                with self._state.lock:
                    self._state.accept_inspection_locked(group, current)
                    group.life.settle_start_completion_locked()
                return current
        return None

    def _archive_fail_stopped_start(
        self,
        group: _Group,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> None:
        """Accept the stopped inspection a guest-network fail-stop left, then archive it."""
        with self._state.lock:
            self._state.accept_inspection_locked(group, inspection)
        self.persist_stopped_archive(group, inspection, members)

    def _fail_stop_uncertain_start(
        self,
        group: _Group,
        members: tuple[SharedMemberManifest, ...],
        expected_container_id: str,
    ) -> None:
        """Exact-stop a start that failed after its durable fence, then archive it.

        Once the durable start fence exists, any untyped manager error is
        ambiguous: Docker may have started the container before guest network
        synchronization or proof failed. Never admit reuse from that state.
        Exact-stop it with the bridge retained and complete the same fail-stop
        archive path; if stopping itself fails, the pending barrier remains for
        reconcile/shutdown to retry.
        """
        with self._state.lock:
            group.life.owe_final_locked(members if self._archives_finals else None)
        uncertain = self._state.require_current_inspection(group)
        if uncertain.running:
            uncertain = self._manager.stop(
                group.spec,
                expected_container_id=expected_container_id,
                release_networks=not self._archives_finals,
            )
        self.persist_stopped_archive(group, uncertain, members)

    def _complete_start(
        self, group: _Group, inspection: ChatSandboxInspection, *, start_fence_prepared: bool
    ) -> None:
        """Clear the start archive fence of a proven start and accept its inspection."""
        with self._state.lock:
            completion_pending = group.start_archive_completion_pending
        if start_fence_prepared or completion_pending:
            self._archive.started(group.spec, inspection)
        with self._state.lock:
            self._state.accept_inspection_locked(group, inspection)
            if start_fence_prepared or completion_pending:
                group.life.settle_start_completion_locked()

    def start_seq_for(self, sandbox_id: str) -> int | None:
        """The start fence of the run a stop of this group ends, if known."""
        with self._state.lock:
            group = self._state.groups.get(sandbox_id)
            if group is None or group.run_unfenced:
                return None
            return group.start_seq

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
        with self._state.lock:
            group, requested = self._state.member(thread_id)
            if group.container_lost:
                raise RetainedChatLost("shared sandbox container identity was lost")
            if group.spec.generation != generation:
                raise SessionLeaseLost("shared sandbox generation is stale")
            if activation_generation == 0 and (
                reason not in {"orphaned_attempt", "lost"} or member_activation_generations is None
            ):
                raise SessionLeaseLost("group-level stop is missing its durable member fence")
            if activation_generation not in (None, 0) and (
                requested.activation_generation != activation_generation
            ):
                raise SessionLeaseLost("shared member activation generation is stale")
            if member_activation_generations is not None and any(
                member_activation_generations.get(member.thread_id) != member.activation_generation
                for member in group.members.values()
            ):
                raise SessionLeaseLost(
                    "shared sandbox member activation changed after stop request"
                )
            group.lifecycle.admit(Operation.IDLE_STOP if reason == "idle" else Operation.STOP)
            if (
                reason == "idle"
                and require_expired_deadline
                and (group.idle_deadline is None or group.idle_deadline > self._state.clock())
            ):
                raise SessionBusy("shared sandbox group inactivity deadline has not elapsed")
            check = self._state.begin_lifecycle_check_locked(group)
        try:
            result = self._stop_checked_group(
                check, requested, reason=reason, defer_archive=defer_archive
            )
        finally:
            with self._state.lock:
                self._state.end_lifecycle_check_locked(check)
        with self._state.lock:
            return replace(result, group_busy=not group.lifecycle.is_quiescent())

    def _stop_checked_group(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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
        with self._state.lock:
            self._state.require_lifecycle_check_locked(check)
            if current is None:
                self._state.mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            self._state.accept_inspection_locked(group, current)
            read_marker = (
                not current.running
                and self._archives_finals
                and not group.stop_archive_blocked
                and not group.stop_archive_pending
            )
        marker_payload = None
        marker_prepared = False
        if read_marker:
            try:
                marker_payload = self.read_final_marker(check.spec, current)
                if marker_payload is None and current.status is not ChatSandboxStatus.CREATED:
                    marker_prepared = self.prepare_markerless_final(check.spec, current)
            except BaseException:
                with self._state.lock:
                    self._state.require_lifecycle_check_locked(check)
                    group.life.owe_orphan_locked(marker_unreadable=True)
                raise
        created_cleanup = False
        quarantine_unreadable = False
        stop: Hold | None = None
        with self._state.lock:
            self._state.require_lifecycle_check_locked(check)
            owned_bridge = self._manager.network.network_for(group.spec)
            blocked = group.stop_archive_blocked
            retry_pending = (
                group.stop_archive_pending
                or group.orphan_finalization_pending
                or group.start_archive_completion_pending
            )
            quarantine_unreadable = current.running and group.orphan_marker_unreadable
            if (
                not current.running
                and self._archives_finals
                and not blocked
                and not group.stop_archive_pending
            ):
                group.life.marker_read_locked()
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
                    group.life.settle_orphan_locked()
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
                    group.life.owe_final_locked(
                        self._state.archive_members_locked(group), blocked=True
                    )
                    retry_pending = True
            if retry_pending and not group.stop_archive_pending and not quarantine_unreadable:
                group.life.owe_final_locked(
                    self._state.archive_members_locked(group) if self._archives_finals else None
                )
                retry_pending = group.stop_archive_pending
            if quarantine_unreadable or blocked:
                stop = group.life.begin_stop_locked(StopHolder.STOP, check=check)
                group.idle_deadline = None
            elif not current.running and owned_bridge not in current.networks and not retry_pending:
                for member in group.members.values():
                    member.resident_state = "lost" if reason == "lost" else "stopped"
                    if reason == "lost":
                        member.context_status = "lost"
                group.idle_deadline = None
                return self._state.descriptor(group, requested)
            elif retry_pending and not created_cleanup:
                stop = group.life.begin_stop_locked(StopHolder.STOP, check=check)
                group.idle_deadline = None
            else:
                stop = group.life.begin_stop_locked(StopHolder.STOP, check=check)
                group.life.advance_generation_locked()
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
                    member.window = None
                    member.lease_token = None
                    member.lease_owner = None
                    member.resident_state = "lost" if reason == "lost" else "stopped"
                    if reason == "lost":
                        member.context_status = "lost"
                archive_members = self._state.archive_members_locked(group)
                expected_container_id = group.inspection.container_id

        if quarantine_unreadable:
            try:
                quarantined = self._manager.stop(
                    group.spec,
                    expected_container_id=current.container_id,
                    release_networks=False,
                )
                with self._state.lock:
                    self._state.accept_inspection_locked(group, quarantined)
                    for member in group.members.values():
                        member.resident_state = "stopped"
                    group.idle_deadline = None
            finally:
                with self._state.lock:
                    group.life.end_stop_locked(stop)
                    group.lifecycle.record_combination(site="end_stop")
            raise SandboxError("shared sandbox final archive marker remains unreadable")

        if blocked:
            try:
                self.stop_blocked_group(group, current)
            finally:
                with self._state.lock:
                    group.life.end_stop_locked(stop)
                    group.lifecycle.record_combination(site="end_stop")
            with self._state.lock:
                if group.stop_archive_blocked:
                    raise SandboxError("shared sandbox final archive skip could not be persisted")
                return self._state.descriptor(group, requested)

        if retry_pending:
            try:
                self.retry_stopped_archive(group, persist=not defer_archive)
            finally:
                with self._state.lock:
                    group.life.end_stop_locked(stop)
                    group.lifecycle.record_combination(site="end_stop")
            with self._state.lock:
                return self._state.descriptor(group, requested)

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
                with self._state.lock:
                    group.life.owe_final_locked(
                        archive_members if self._archives_finals else None, blocked=True
                    )
                inspection = self.stop_blocked_group(group, current)
                with self._state.lock:
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
                if self._archives_finals:
                    with self._state.lock:
                        group.life.owe_final_locked(archive_members)
                try:
                    self._archive.prepare_final(group.spec, current)
                except Exception:
                    if not defer_archive:
                        raise
                    # Network proof is already lost. Archive preparation
                    # failure cannot leave this group running while later
                    # groups wait. Skip this lifecycle's final archive and
                    # finish the exact stop without upload.
                    with self._state.lock:
                        group.life.owe_final_locked(archive_members, blocked=True)
                    inspection = self.stop_blocked_group(group, current)
                    with self._state.lock:
                        skip_persisted = not group.stop_archive_blocked
                    if not skip_persisted:
                        raise
                if inspection is None:
                    inspection = self._manager.stop(
                        group.spec,
                        expected_container_id=expected_container_id,
                        release_networks=not self._archives_finals,
                    )
                    # This is synchronous and fail-closed. A failed upload remains
                    # pinned to the same policy revision and leaves the stopped
                    # container/network as the durable retry marker.
                    if not defer_archive:
                        inspection = self.persist_stopped_archive(
                            group,
                            inspection,
                            archive_members,
                        )
        except BaseException:
            failed_inspection: ChatSandboxInspection | None = None
            inspected = False
            with contextlib.suppress(Exception):
                failed_inspection = self._manager.inspect(group.spec.container_name)
                inspected = True
            with self._state.lock:
                if inspection is not None:
                    self._state.accept_inspection_locked(group, inspection)
                    for member in group.members.values():
                        member.resident_state = "lost" if reason == "lost" else "stopped"
                        if reason == "lost":
                            member.context_status = "lost"
                    group.idle_deadline = None
                elif not inspected:
                    # Only Docker's "no such container" proves the container
                    # gone; a daemon that cannot answer proves nothing. Keep the
                    # last accepted observation for the next sweep or
                    # reconciliation to inspect again.
                    if not group.container_lost:
                        for member in group.members.values():
                            member.resident_state = (
                                "restarting" if member.native_session_id else "stopped"
                            )
                elif failed_inspection is None:
                    self._state.mark_container_lost_locked(group)
                else:
                    try:
                        self._state.accept_inspection_locked(group, failed_inspection)
                    except RetainedChatLost:
                        pass
                    else:
                        for member in group.members.values():
                            member.resident_state = (
                                "restarting" if member.native_session_id else "stopped"
                            )
                group.life.end_stop_locked(stop)
                group.lifecycle.record_combination(site="end_stop")
                if inspection is None:
                    self._state.arm_idle_if_quiescent(group, allow_lifecycle_check=True)
            raise
        assert inspection is not None
        with self._state.lock:
            self._state.accept_inspection_locked(group, inspection)
            for member in group.members.values():
                member.resident_state = "lost" if reason == "lost" else "stopped"
                if reason == "lost":
                    member.context_status = "lost"
            group.life.end_stop_locked(stop)
            group.lifecycle.record_combination(site="end_stop")
            group.idle_deadline = None
            return self._state.descriptor(group, requested)

    def begin_archive(
        self,
        thread_id: str,
        *,
        request_id: str,
        generation: int,
        container_id: str,
    ) -> tuple[ChatSandboxSpec, dict[str, Any]]:
        """Hold a stopped group's admission barrier across S3 verification."""
        with self._state.lock:
            group, _ = self._state.member(thread_id)
            if group.spec.generation != generation:
                raise SessionLeaseLost("sandbox archive generation is stale")
            if group.archive_request_id not in (None, request_id):
                raise SessionBusy("another sandbox archive is in progress")
            group.lifecycle.admit(
                Operation.ARCHIVE if group.archive_request_id is None else Operation.DELETION
            )
            check = self._state.begin_lifecycle_check_locked(group)
        try:
            current = self._manager.require_stopped_archive(check.spec, container_id=container_id)
            with self._state.lock:
                self._state.require_lifecycle_check_locked(check)
                self._state.accept_inspection_locked(group, current)
                if group.life.hold_archive_locked(request_id):
                    group.life.advance_generation_locked()
                group.idle_deadline = None
                return group.spec, {
                    "spec": asdict(group.spec),
                    "inspection": asdict(current),
                    "members": [asdict(item) for item in self._state.descriptors(group)],
                }

        finally:
            with self._state.lock:
                self._state.end_lifecycle_check_locked(check)

    def abort_archive(self, spec: ChatSandboxSpec, *, request_id: str, container_id: str) -> None:
        """Release only the caller's archival barrier after a failed operation."""
        with self._state.lock:
            group = self._state.groups.get(spec.sandbox_id)
            if group is None:
                return
            if group.spec != spec or group.inspection.container_id != container_id:
                raise SessionLeaseLost("sandbox archive identity changed")
            if group.archive_request_id not in (None, request_id):
                raise SessionLeaseLost("sandbox archive request is stale")
            group.lifecycle.admit(Operation.LIFECYCLE_CHECK)
            if group.archive_request_id == request_id:
                group.life.release_archive_locked(request_id)
                self._state.arm_idle_if_quiescent(group)

    def commit_archive(self, spec: ChatSandboxSpec, *, request_id: str, container_id: str) -> None:
        """Remove a stopped group after durable authorization; cleanup is replayable."""
        with self._state.lock:
            group = self._state.groups.get(spec.sandbox_id)
            check = self._begin_commit_locked(
                spec, group, request_id=request_id, container_id=container_id
            )
        try:
            current = self._manager.inspect(spec.container_name)
            with self._state.lock:
                if check is not None:
                    self._state.require_lifecycle_check_locked(check)
                if current is not None and group is not None:
                    group.lifecycle.admit(
                        Operation.IDLE_DELETION
                        if group.archive_request_id is None
                        else Operation.DELETION,
                        allow_lifecycle_check=True,
                    )
                    self._state.accept_inspection_locked(group, current)
                    group.life.hold_archive_locked(request_id)
                    group.idle_deadline = None
            if current is not None:
                self._manager.remove_stopped_archived(spec, container_id=container_id)
            # A renamed original or stable-name replacement cannot prove deletion.
            original = self._manager.inspect(container_id)
            replacement = self._manager.inspect(spec.container_name)
            with self._state.lock:
                if check is not None:
                    self._state.require_lifecycle_check_locked(check)
                    if (
                        group is not None
                        and group.archive_request_id != request_id
                        and current is not None
                    ):
                        raise SessionLeaseLost("sandbox archive request is stale")
                if original is not None or replacement is not None:
                    raise RetainedChatLost("archived sandbox identity changed during deletion")
            forget_deleted_container_uploads(spec.container_name)
            with self._state.lock:
                if check is not None:
                    self._state.require_lifecycle_check_locked(check)
                if group is not None:
                    self._forget_deleted_group_locked(spec, group)
                self._state.condition.notify_all()
        finally:
            with self._state.lock:
                if check is not None:
                    self._state.end_lifecycle_check_locked(check)
                else:
                    self._state.creating.discard(spec.sandbox_id)
                    self._state.condition.notify_all()

    def _begin_commit_locked(
        self,
        spec: ChatSandboxSpec,
        group: _Group | None,
        *,
        request_id: str,
        container_id: str,
    ) -> _LifecycleCheck | None:
        """Fence an archive commit to its group, or reserve the sandbox id without one.

        The caller holds the lock.
        """
        if group is not None:
            if group.spec != spec or group.inspection.container_id != container_id:
                raise SessionLeaseLost("sandbox archive identity changed")
            if group.archive_request_id not in (None, request_id):
                raise SessionLeaseLost("sandbox archive request is stale")
            if group.archive_request_id is None and not group.container_lost:
                # Refusing a busy deletion must not invalidate an active
                # native fork merely by advancing the lifecycle clock.
                group.lifecycle.admit(Operation.IDLE_DELETION)
            return self._state.begin_lifecycle_check_locked(group)
        if spec.sandbox_id in self._state.creating:
            raise SessionBusy("shared workspace creation is in progress")
        self._state.creating.add(spec.sandbox_id)
        return None

    def _forget_deleted_group_locked(self, spec: ChatSandboxSpec, group: _Group) -> None:
        """Mark a deleted group's members lost and drop it from the registry.

        The caller holds the lock.
        """
        for member in group.members.values():
            member.context_status = "lost"
            member.resident_state = "lost"
            self._state.members.pop(member.thread_id, None)
        self._state.groups.pop(spec.sandbox_id, None)

    def start_and_prove(
        self,
        group: _Group,
        *,
        start_seq: int | _SameRun | None = _SAME_RUN,
        facts: SandboxActionFacts | None = None,
        observer: StageRecorder | None = None,
    ) -> ChatSandboxInspection:
        """Start the group if it is stopped, and prove its network once for one request.

        A start proves it; a retried archive completion starts nothing, so the
        running container it reused is proved here instead.
        """
        inspection, proved = self.start_group(
            group, start_seq=start_seq, facts=facts, observer=observer
        )
        if not proved:
            self._manager.require_network(group.spec, observer=observer)
        return inspection

    def ensure_running(
        self, group: _Group, *, start_seq: int | _SameRun | None = _SAME_RUN
    ) -> None:
        """Start the group if it is stopped, and prove its network for this request."""
        ensure_running(self, group, start_seq=start_seq)

    def recover_start_archive_completion(self, group: _Group) -> None:
        """Retry an idempotent post-start marker completion before admission."""
        with self._state.lock:
            pending = group.start_archive_completion_pending
        if pending:
            self.start_group(group)

    def _exact_stop_unguarded(self, workload: GuardedWorkload) -> bool:
        """Stop one discovered container by exact id, retaining archive state when needed."""
        return exact_stop_unguarded(
            self._manager,
            workload,
            release_networks=workload.environment_run or not self._archives_finals,
        )

    def guard_network(self) -> builtins.list[SharedSessionDescriptor]:
        """Fail-stop every running workload when the host cannot prove its rules.

        Discovery is host-wide, including environment runs and groups already
        stopping or not yet reconciled. Retained groups keep their normal final
        archive lifecycle and pending-archive bridge.
        """
        inventory = running_guarded_workloads(self._manager)
        with self._state.lock:
            workloads = workloads_with_known(inventory, self._state.groups.values())
        if not workloads and inventory.complete and not inventory.unresolved_container_ids:
            self._idle_host_rules.renew()
            return []
        failure = host_rule_failure(self._manager, workloads)
        if failure is None:
            return []
        inventory = merge_guarded_inventories(
            inventory,
            running_guarded_workloads(self._manager),
        )
        with self._state.lock:
            workloads = workloads_with_known(inventory, self._state.groups.values())
        retry_unresolved_stops(self._manager, inventory)
        logger.error(
            "shared_chat_network_unguarded",
            extra={
                "event": "shared_chat_network_unguarded",
                "groups": len(workloads),
                "inventory_complete": inventory.complete,
                "unidentified": len(inventory.unresolved_container_ids),
                "detail": failure,
            },
        )

        managed: builtins.list[tuple[str, int, GuardedWorkload]] = []
        direct: builtins.list[GuardedWorkload] = []
        with self._state.lock:
            for workload in workloads:
                group = self._state.groups.get(workload.spec.sandbox_id)
                if (
                    workload.environment_run
                    or group is None
                    or group.spec != workload.spec
                    or group.inspection.container_id != workload.inspection.container_id
                    or group.stopping
                    or group.starting
                    or not group.members
                ):
                    direct.append(workload)
                else:
                    managed.append((next(iter(group.members)), workload.spec.generation, workload))

        # Stop independent and already-stopping workloads before any retained
        # group spends time flushing native state or preparing an archive.
        for workload in direct:
            self._exact_stop_unguarded(workload)

        staged: builtins.list[tuple[str, int]] = []
        for thread_id, generation, workload in managed:
            try:
                self.stop_if_idle(
                    thread_id,
                    generation=generation,
                    reason="unguarded",
                    defer_archive=True,
                )
            except Exception:
                logger.exception(
                    "unguarded shared sandbox lifecycle stop failed",
                    extra={
                        "event": "shared_chat_network_fail_stop_failed",
                        "thread_id": thread_id,
                        "generation": generation,
                    },
                )
                # A concurrent stop may have raised SessionBusy after discovery.
                # Its exact container still must not keep running unguarded.
                self._exact_stop_unguarded(workload)
            else:
                staged.append((thread_id, generation))

        # Every exact Docker stop completes before compression or upload starts.
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


__all__ = [
    "GroupLifecycleController",
]
