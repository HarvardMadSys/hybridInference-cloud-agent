"""Reconcile the host's shared containers with the supervisor's groups, and observe them.

Reconciliation adopts a shared container this process has not seen and
finishes, from its durable marker, a final archive an earlier process left
pending. Each group's slow recovery runs under its own lifecycle reservation,
in the request or queued to :class:`SharedSessionRecovery`. The observations
here inspect Docker without the metadata lock and never replace an observation
that a lifecycle call made meanwhile.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from cloud_agent_host.archive.archive_failure_diagnostics import (
    _archive_failure_diagnostic,
)
from cloud_agent_host.container.chat_sandbox import (
    CHAT_SHARED_LABEL,
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatLost,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.network.guarded_workloads import (
    sandbox_spec_from_inspection,
)
from cloud_agent_host.session_lifecycle import (
    SessionLeaseLost,
)
from cloud_agent_host.supervisor.group_lifecycle import Operation
from cloud_agent_host.supervisor.group_lifecycle_state import StopHolder
from cloud_agent_host.supervisor.shared_session_recovery import SharedSessionRecovery
from cloud_agent_host.supervisor.shared_session_state import (
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionState,
    _Group,
    _LifecycleCheck,
    _Member,
)
from cloud_agent_shared.errors import SandboxArchiveBlocked

if TYPE_CHECKING:
    import builtins

    from cloud_agent_host.drivers import DialectRuntime
    from cloud_agent_host.resident_transport import (
        ResidentTransport,
    )
    from cloud_agent_host.supervisor.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.supervisor.group_lifecycle_state import Hold

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


class GroupReconciler:
    """Discover, recover and observe groups, each under its own reservation."""

    def __init__(self, state: SharedSessionState, *, lifecycle: GroupLifecycleController) -> None:
        self._state = state
        self._manager = state.manager
        self._archive = state.archive
        self._archives_finals = state.archives_finals
        self._lifecycle = lifecycle
        self._recovery = SharedSessionRecovery(state, self.reconcile_checked_group)

    @property
    def recovery(self) -> SharedSessionRecovery:
        """The owned recovery that runs queued reconciliations off request workers."""
        return self._recovery

    def reconcile(
        self, *, background_recovery: bool = False
    ) -> builtins.list[SharedSessionDescriptor]:
        """Discover containers, fencing each group's slow recovery independently."""
        with self._state.lock:
            before = {
                key: (group, group.life.generation) for key, group in self._state.groups.items()
            }
        found: builtins.list[SharedSessionDescriptor] = []
        specs: builtins.list[ChatSandboxSpec] = []
        for inspection in (listed := self._manager.list()):
            if inspection.labels.get(CHAT_SHARED_LABEL) != "true":
                continue
            spec = sandbox_spec_from_inspection(inspection)
            specs.append(spec)
            with self._state.lock:
                group = self._state.groups.get(spec.sandbox_id)
                previous = before.get(spec.sandbox_id)
                if group is not None and (
                    (
                        previous is None
                        or previous[0] is not group
                        or previous[1] != group.life.generation
                    )
                    or group.life.check is not None
                    or group.life.start is not None
                    or group.life.stop is not None
                ):
                    found.extend(self._state.descriptors(group))
                    continue
                if group is None and spec.sandbox_id in self._state.creating:
                    continue
                discovered = group is None
                if group is None:
                    group = _Group(
                        spec=spec,
                        inspection=inspection,
                        life=self._state.new_lifecycle(
                            sealed_seq=self._archive.checkpoint_seq(spec, inspection)
                        ),
                    )
                    origin = _Member(thread_id=spec.thread_id, resident_slot_id=0)
                    group.members[origin.thread_id] = origin
                    self._state.groups[spec.sandbox_id] = group
                    self._state.members[origin.thread_id] = group
                else:
                    try:
                        if group.spec != spec:
                            raise SandboxError("shared sandbox immutable labels changed")
                        self._state.accept_inspection_locked(group, inspection)
                    except SandboxError as exc:
                        # A lost or changed group keeps its last observation and
                        # is reported as it stands; it must not cost every other
                        # group on this host its listing.
                        logger.warning(
                            "shared sandbox group was not reconciled",
                            extra={
                                "event": "shared_sandbox_reconcile_group_failed",
                                "sandbox_id": spec.sandbox_id,
                                "generation": spec.generation,
                                "detail": str(exc)[:300],
                            },
                        )
                        found.extend(self._state.descriptors(group))
                        continue
                read_marker = (
                    discovered
                    or group.life.orphan_marker_unreadable
                    or (
                        not inspection.running
                        and inspection.status is not ChatSandboxStatus.CREATED
                        and self._manager.network.network_for(spec) in inspection.networks
                        and group.lifecycle.is_quiescent()
                        and not group.life.final_archive_blocked
                        and group.life.orphan is None
                        and not group.life.final_archive_pending
                    )
                )
                if not (
                    read_marker
                    or group.life.final_archive_blocked
                    or group.life.orphan is not None
                    or group.life.final_archive_pending
                ):
                    found.extend(self._state.descriptors(group))
                    continue
                check = self._state.begin_lifecycle_check_locked(group)
            reconciled = self._recovery.reconcile(
                check,
                inspection,
                discovered=discovered,
                read_marker=read_marker,
                background=background_recovery,
            )
            with self._state.lock:
                if reconciled is None:
                    if self._state.groups.get(spec.sandbox_id) is group:
                        found.extend(self._state.descriptors(group))
                    continue
                found.extend(
                    replace(item, group_busy=not group.lifecycle.is_quiescent())
                    for item in reconciled
                )
        self._manager.network.prune_orphan_networks(specs, listed)
        return found

    def reconcile_checked_group(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        self,
        check: _LifecycleCheck,
        inspection: ChatSandboxInspection,
        *,
        discovered: bool,
        read_marker: bool,
    ) -> builtins.list[SharedSessionDescriptor]:
        """Classify and recover the reserved exact group without holding the global lock."""
        group, spec = check.group, check.spec
        network = self._manager.network
        found: builtins.list[SharedSessionDescriptor] = []
        marker_payload = None
        marker_error = None
        marker_prepared = False
        if read_marker:
            try:
                marker_payload = self._lifecycle.read_final_marker(spec, inspection)
                if (
                    marker_payload is None
                    and inspection.status is not ChatSandboxStatus.CREATED
                    and (inspection.running or network.network_for(spec) in inspection.networks)
                ):
                    marker_prepared = self._lifecycle.prepare_markerless_final(spec, inspection)
            except BaseException as exc:
                marker_error = exc
        owned_bridge = network.network_for(spec)
        finalize = False
        retry = False
        blocked = False
        invalidate_unknown = False
        marker_unreadable = False
        members: tuple[SharedMemberManifest, ...] = ()
        stop: Hold | None = None
        with self._state.lock:
            self._state.require_lifecycle_check_locked(check)
            if discovered:
                try:
                    marker_payload = self._lifecycle.marker_result(marker_payload, marker_error)
                except BaseException:
                    # A marker read is an authority check, not optional
                    # observability. Keep the adopted group fenced if its
                    # sandbox-manager-private lifecycle record is unreadable, but do
                    # not let one damaged orphan prevent healthy groups from
                    # reconciling and receiving the network guard.
                    group.life.owe_orphan_locked(marker_unreadable=True)
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
                        self._archives_finals
                        and not marker_present
                        and inspection.status is not ChatSandboxStatus.CREATED
                        and (inspection.running or owned_bridge in inspection.networks)
                    )
                    if unknown_unflushed:
                        members = self._state.archive_members_locked(group)
                        group.life.owe_final_locked(members, blocked=True)
                        if inspection.running:
                            stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
                        blocked = True
                        if inspection.running:
                            invalidate_unknown = True
                    else:
                        # With a marker, its original policy and phase are
                        # authoritative even if an external actor removed the
                        # bridge. Without archive hooks, retain legacy cleanup.
                        finalize = marker_present or (
                            not self._archives_finals
                            and (inspection.running or owned_bridge in inspection.networks)
                        )
                    if finalize:
                        group.life.owe_orphan_locked()
                        stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
                        members = self._state.archive_members_locked(group)
            else:
                if group.spec != spec:
                    raise SandboxError("shared sandbox immutable labels changed")
                self._state.accept_inspection_locked(group, inspection)
                if group.life.orphan_marker_unreadable:
                    try:
                        marker_payload = self._lifecycle.marker_result(marker_payload, marker_error)
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
                        group.life.marker_read_locked()
                        marker_present = marker_payload is not None
                        if (
                            not marker_present
                            and inspection.status is not ChatSandboxStatus.CREATED
                            and (inspection.running or owned_bridge in inspection.networks)
                        ):
                            marker_present = marker_prepared
                        unknown_unflushed = (
                            self._archives_finals
                            and not marker_present
                            and inspection.status is not ChatSandboxStatus.CREATED
                            and (inspection.running or owned_bridge in inspection.networks)
                        )
                        if unknown_unflushed:
                            members = self._state.archive_members_locked(group)
                            group.life.owe_final_locked(members, blocked=True)
                            group.life.settle_orphan_locked()
                            if inspection.running:
                                stop = group.life.begin_stop_locked(
                                    StopHolder.FINALIZE, check=check
                                )
                            blocked = True
                            if inspection.running:
                                invalidate_unknown = True
                        elif marker_present:
                            stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
                            finalize = True
                            members = self._state.archive_members_locked(group)
                        else:
                            group.life.settle_orphan_locked()
                elif group.life.final_archive_blocked:
                    if inspection.running:
                        stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
                    blocked = True
                elif group.life.orphan is not None:
                    group.lifecycle.admit(Operation.ORPHAN_FINALIZATION)
                    stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
                    finalize = True
                    members = self._state.archive_members_locked(group)
                elif group.life.final_archive_pending:
                    group.lifecycle.admit(Operation.TAKE_STOP)
                    stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
                    retry = True
                elif (
                    not inspection.running
                    and inspection.status is not ChatSandboxStatus.CREATED
                    and owned_bridge in inspection.networks
                    and group.lifecycle.is_quiescent(allow_lifecycle_check=True)
                ):
                    # Bridge presence alone is not authority to invent a
                    # clean final archive. Classify the durable marker first:
                    # present means retry its exact lifecycle; absent means
                    # native history is unknown and must remain blocked.
                    try:
                        marker_payload = self._lifecycle.marker_result(marker_payload, marker_error)
                    except BaseException:
                        group.life.owe_orphan_locked(marker_unreadable=True)
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
                        members = self._state.archive_members_locked(group)
                        marker_present = marker_payload is not None
                        if not marker_present:
                            marker_present = marker_prepared
                        if not marker_present and self._archives_finals:
                            group.life.owe_final_locked(members, blocked=True)
                            blocked = True
                        else:
                            group.lifecycle.admit(Operation.TAKE_STOP)
                            stop = group.life.begin_stop_locked(StopHolder.FINALIZE, check=check)
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
                    with self._state.lock:
                        self._state.accept_inspection_locked(group, inspection)
            with self._state.lock:
                found.extend(self._state.descriptors(group))
            return found

        if invalidate_unknown:
            try:
                self._archive.invalidate_final(spec, inspection)
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
                inspection = self._lifecycle.stop_blocked_group(group, inspection)
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
                with self._state.lock:
                    group.life.end_stop_locked(stop)
                    found.extend(self._state.descriptors(group))
            return found

        # Docker stop, compression, and S3 upload may take minutes. The
        # per-group ``stopping`` barrier rejects competing activation while
        # allowing unrelated groups' status and lifecycle calls to proceed.
        if retry or finalize:
            try:
                if retry:
                    self._lifecycle.retry_stopped_archive(group)
                else:
                    if inspection.running:
                        self._archive.prepare_final(spec, inspection)
                    inspection = self._manager.stop(
                        spec,
                        expected_container_id=inspection.container_id,
                        release_networks=not self._archives_finals,
                    )
                    inspection = self._lifecycle.persist_stopped_archive(group, inspection, members)
                    with self._state.lock:
                        group.life.settle_orphan_locked()
            except SandboxArchiveBlocked:
                with self._state.lock:
                    group.life.owe_final_locked(members, blocked=True)
                    group.life.settle_orphan_locked()
                try:
                    if inspection.running:
                        inspection = self._manager.stop(
                            spec,
                            expected_container_id=inspection.container_id,
                            release_networks=False,
                        )
                    with self._state.lock:
                        self._state.accept_inspection_locked(group, inspection)
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
                with self._state.lock:
                    group.life.owe_orphan_locked(marker_unreadable=self._archives_finals)
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
                        with self._state.lock:
                            self._state.accept_inspection_locked(group, inspection)
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
                with self._state.lock:
                    group.life.end_stop_locked(stop)
        with self._state.lock:
            found.extend(self._state.descriptors(group))
        return found

    def status(self, thread_id: str) -> SharedSessionDescriptor:
        """Return fresh Docker and member process state.

        Docker is asked outside the metadata lock, which every terminal request
        would otherwise hold across an inspect. An observation that a lifecycle
        call superseded while it was in flight never replaces that call's own.
        """
        stale_transport: ResidentTransport[DialectRuntime] | None = None
        with self._state.lock:
            group, member = self._state.member(thread_id)
            observed = group.inspection
            lifecycle_generation = group.life.generation
        current = self._manager.inspect(group.spec.container_name)
        with self._state.lock:
            current_group, current_member = self._state.member(thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost("shared sandbox membership changed during observation")
            if group.inspection == observed and group.life.generation == lifecycle_generation:
                if current is None:
                    member.context_status = "lost"
                    member.resident_state = "lost"
                    raise RetainedChatLost("shared sandbox container is missing")
                self._state.accept_inspection_locked(group, current)
            elif group.life.container_lost:
                raise RetainedChatLost("shared sandbox container identity was lost")
            stale_transport = self._detach_dead_transport(member)
            descriptor = self._state.descriptor(group, member)
        if stale_transport is not None:
            with contextlib.suppress(Exception):
                stale_transport.close()
        return descriptor

    def list(self, *, background_recovery: bool = False) -> builtins.list[SharedSessionDescriptor]:
        """Observe memberships; HTTP readers queue slow lifecycle recovery."""
        self.reconcile(background_recovery=background_recovery)
        stale_transports: builtins.list[ResidentTransport[DialectRuntime]] = []
        with self._state.lock:
            for group in self._state.groups.values():
                for member in group.members.values():
                    stale = self._detach_dead_transport(member)
                    if stale is not None:
                        stale_transports.append(stale)
            descriptors = [
                self._state.descriptor(group, member)
                for group in self._state.groups.values()
                for member in group.members.values()
            ]
        for transport in stale_transports:
            with contextlib.suppress(Exception):
                transport.close()
        return descriptors

    def list_for_thread(self, thread_id: str) -> builtins.list[SharedSessionDescriptor]:
        """Observe one known group without running host-wide reconciliation."""
        with self._state.lock:
            group, member = self._state.member(thread_id)
            spec = group.spec
            lifecycle_generation = group.life.generation
            inspection = group.inspection
            container_id = inspection.container_id

        current = self._manager.inspect(spec.container_name)
        stale_transports: builtins.list[ResidentTransport[DialectRuntime]] = []
        with self._state.lock:
            # A concurrent start can refresh inspection without advancing the
            # lifecycle generation. Never overwrite its accepted Docker state.
            if (
                self._state.groups.get(spec.sandbox_id) is not group
                or self._state.members.get(thread_id) is not group
                or group.members.get(thread_id) is not member
                or group.spec != spec
                or group.life.generation != lifecycle_generation
                or group.inspection != inspection
            ):
                raise SessionLeaseLost("shared sandbox lifecycle changed during observation")
            if current is None:
                self._state.mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container is missing")
            if current.container_id != container_id:
                self._state.mark_container_lost_locked(group)
                raise RetainedChatLost("shared sandbox container identity changed")
            if sandbox_spec_from_inspection(current) != spec:
                raise SandboxError("shared sandbox immutable labels changed")
            self._state.accept_inspection_locked(group, current)
            for group_member in group.members.values():
                stale = self._detach_dead_transport(group_member)
                if stale is not None:
                    stale_transports.append(stale)
            descriptors = self._state.descriptors(group)
        for transport in stale_transports:
            with contextlib.suppress(Exception):
                transport.close()
        return descriptors

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


__all__ = [
    "GroupReconciler",
]
