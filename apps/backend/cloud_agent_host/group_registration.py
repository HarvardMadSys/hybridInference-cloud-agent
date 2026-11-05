"""Install the control plane's manifest: create, adopt or restore a group, then its members.

A registration that creates or adopts a container does it without the metadata
lock; the sandbox is listed in :attr:`SharedSessionState.creating` meanwhile,
so only registrations of that sandbox wait for it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    RetainedChatLost,
    SandboxActionFacts,
    SandboxRestoreFailed,
)
from cloud_agent_host.group_lifecycle import Operation
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_checkpoints import CheckpointArchiveInvalid, CheckpointRestore
from cloud_agent_host.sandbox_deletion_fence import prove_admin_deletion_target_absent
from cloud_agent_host.session_lifecycle import (
    SessionBusy,
    SessionLeaseLost,
)
from cloud_agent_host.shared_session_state import (
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionState,
    _Group,
    _Member,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef
    from cloud_agent_shared.operation_profiling import (
        StageRecorder,
    )
    from cloud_agent_shared.sandbox_manager_wire import ResidentState

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


class GroupRegistration:
    """Register groups and their members from the control plane's manifests."""

    def __init__(self, state: SharedSessionState) -> None:
        self._state = state
        self._manager = state.manager
        self._archive = state.archive

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
        """Install the control plane's authoritative member aliases.

        A group with no container here is created; a moved group (one the
        control plane rehomed under a later generation) is created only from
        ``restore``, its checkpoint, and never empty. A new group with an
        ``environment_snapshot`` starts from it when it can, and from the
        sandbox image otherwise.
        """
        validated_members = self._validated_members(spec, members)
        with self._state.lock:
            deadline = time.monotonic() + 30.0
            group = self._state.groups.get(spec.sandbox_id)
            if group is not None and group.spec.generation < spec.generation:
                # The control plane moved this group under a newer generation;
                # whatever this host remembers of the old one is fenced out.
                group = self._forget_superseded_locked(group)
            while group is None and spec.sandbox_id in self._state.creating:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionBusy("shared workspace creation is in progress")
                self._state.condition.wait(timeout=min(remaining, 0.5))
                group = self._state.groups.get(spec.sandbox_id)
            if group is not None:
                while group.lifecycle_check is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SessionBusy("shared sandbox lifecycle check is in progress")
                    self._state.condition.wait(timeout=min(remaining, 0.5))
                    if self._state.groups.get(spec.sandbox_id) is not group:
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
            if not expected_container_id and restore is None and spec.generation > 1:
                raise SandboxError(
                    "a moved shared sandbox can only be recreated from its checkpoint"
                )
            self._state.creating.add(spec.sandbox_id)
        # Docker create, or the exact adoption check, can take minutes. Only registrations of this sandbox wait for it; the
        # metadata lock stays free for every other group and member.
        try:
            created, snapshot_failure = self._create_group_container(
                spec,
                expected_container_id=expected_container_id,
                restore=restore,
                environment_snapshot=environment_snapshot,
                facts=facts,
                observer=observer,
            )
        except BaseException:
            with self._state.lock:
                self._state.creating.discard(spec.sandbox_id)
                self._state.condition.notify_all()
            raise
        with self._state.lock:
            self._state.creating.discard(spec.sandbox_id)
            self._state.condition.notify_all()
            group = self._state.groups.get(spec.sandbox_id)
            if group is None:
                group = _Group(
                    spec=spec,
                    inspection=created,
                    environment_snapshot_failure=snapshot_failure,
                    sealed_seq=self._archive.checkpoint_seq(spec, created),
                )
                self._state.groups[spec.sandbox_id] = group
            return self._install_manifest_locked(
                group,
                spec,
                members,
                validated_members,
                expected_container_id=expected_container_id,
            )

    @staticmethod
    def _validated_members(
        spec: ChatSandboxSpec, members: builtins.list[SharedMemberManifest]
    ) -> builtins.list[tuple[SharedMemberManifest, int]]:
        """Pair each manifest member with its resident slot, refusing an invalid manifest."""
        if not members:
            raise ValueError("a shared sandbox manifest cannot be empty")
        seen_slots: set[int] = set()
        # The validation pass rejects a manifest with no resident slot before
        # anything is mutated, so the install pass reads the slot from this
        # pair rather than re-deciding what the check here established.
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
        return validated_members

    def _create_group_container(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None,
        restore: CheckpointRestore | None,
        environment_snapshot: EnvironmentSnapshotRef | None,
        facts: SandboxActionFacts | None,
        observer: StageRecorder | None,
    ) -> tuple[ChatSandboxInspection, str | None]:
        """Adopt, restore or create the group's container, and why a snapshot went unused."""
        if expected_container_id:
            return self._manager.require_existing(spec, container_id=expected_container_id), None
        if restore is not None:
            return self._restore_group(spec, restore, facts=facts, observer=observer), None
        if environment_snapshot is not None:
            return self._create_from_snapshot(
                spec, environment_snapshot, facts=facts, observer=observer
            )
        return self._manager.create(spec, facts=facts, observer=observer), None

    def _restore_group(
        self,
        spec: ChatSandboxSpec,
        restore: CheckpointRestore,
        *,
        facts: SandboxActionFacts | None,
        observer: StageRecorder | None,
    ) -> ChatSandboxInspection:
        """Recreate a moved group's container from its checkpoint, stopped.

        Any failure is reported as :class:`SandboxRestoreFailed`, which hands
        the turn back rather than marking the chat lost: the checkpoint is
        unchanged. What the attempt made goes with it: ``create`` removes a
        container it made that failed its checks, and the image this restore
        loaded is removed here. Any host may claim the next try, and nothing
        would tell this one that the group lives elsewhere.
        """
        if not self._archive.restores_checkpoints:
            raise SandboxRestoreFailed("this sandbox manager cannot restore checkpoints")
        try:
            self._manager.remove_superseded(spec, keep_checkpoint=restore.checkpoint_id)
            image = self._archive.restore_checkpoint(spec.sandbox_id, restore)
            return self._manager.create(
                spec,
                image=image,
                checkpoint_id=restore.checkpoint_id,
                facts=facts,
                observer=observer,
            )
        except SandboxRestoreFailed:
            raise
        except Exception as exc:
            logger.warning(
                "sandbox checkpoint restore failed",
                extra={
                    "event": "sandbox_restore_failed",
                    "sandbox_id": spec.sandbox_id,
                    "generation": spec.generation,
                    "checkpoint_id": restore.checkpoint_id,
                    "error_type": type(exc).__name__,
                },
            )
            # Nothing is kept for a retry here: an older container goes with
            # its image even when it is this checkpoint's, and so does the
            # image this restore loaded. Docker keeps one a container uses.
            with contextlib.suppress(Exception):
                self._manager.remove_superseded(spec)
            with contextlib.suppress(Exception):
                self._manager.remove_checkpoint_image(spec, restore.checkpoint_id)
            raise SandboxRestoreFailed(f"sandbox checkpoint could not be restored: {exc}") from exc

    def _create_from_snapshot(
        self,
        spec: ChatSandboxSpec,
        snapshot: EnvironmentSnapshotRef,
        *,
        facts: SandboxActionFacts | None,
        observer: StageRecorder | None,
    ) -> tuple[ChatSandboxInspection, str | None]:
        """Create a new group from an installation snapshot, or from the image if it fails.

        A snapshot only saves time: whatever goes wrong with it, the group is
        created the ordinary way and its workspace installs in full. The
        returned reason tells the control plane whether the snapshot itself is
        at fault (``invalid``) or only could not be used here (``unavailable``).
        """
        failure = "unavailable"
        try:
            image = self._archive.environment_snapshot(snapshot)
            if image is not None:
                return (
                    self._manager.create(
                        spec,
                        image=image,
                        environment_snapshot=(snapshot.owner, snapshot.snapshot_id),
                        facts=facts,
                        observer=observer,
                    ),
                    None,
                )
        except CheckpointArchiveInvalid:
            failure = "invalid"
        except Exception as exc:
            logger.warning(
                "environment snapshot unavailable; creating from the sandbox image",
                extra={
                    "event": "environment_snapshot_unavailable",
                    "sandbox_id": spec.sandbox_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "error_type": type(exc).__name__,
                },
            )
        return self._manager.create(spec, facts=facts, observer=observer), failure

    def _forget_superseded_locked(self, group: _Group) -> _Group | None:
        """Drop an older generation of a group, unless something here still uses it."""
        if any(
            member.lease_token or member.current_turn is not None or member.transport
            for member in group.members.values()
        ):
            raise SessionBusy("an older generation of this shared sandbox is still in use")
        self._state.groups.pop(group.spec.sandbox_id, None)
        for thread_id in list(group.members):
            if self._state.members.get(thread_id) is group:
                self._state.members.pop(thread_id, None)
        self._state.condition.notify_all()
        return None

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
        group.lifecycle.admit(Operation.MANIFEST)

        wanted = {item.thread_id for item in members}
        for manifest, resident_slot_id in validated_members:
            other_group = self._state.members.get(manifest.thread_id)
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
                member = self._new_member(manifest, resident_slot_id)
                group.members[manifest.thread_id] = member
                self._state.members[manifest.thread_id] = group
            else:
                if member.resident_slot_id != manifest.resident_slot_id:
                    raise SandboxError("member resident slot changed in place")
                if member.lease_token is None and member.current_turn is None:
                    self._refresh_idle_member_locked(member, manifest)
        # Removal is safe only for a manifest member with no live local
        # ownership. A stale manifest never tears down an active process.
        for thread_id, member in list(group.members.items()):
            if thread_id in wanted or member.lease_token or member.transport:
                continue
            group.members.pop(thread_id, None)
            self._state.members.pop(thread_id, None)
        return self._state.descriptors(group)

    @staticmethod
    def _new_member(manifest: SharedMemberManifest, resident_slot_id: int) -> _Member:
        """Build the local record of a manifest member this host has not seen."""
        # A durable observation that the previous sandbox manager owned a live
        # process is not an attachable local transport. Docker preserves the
        # native session files, but the replacement sandbox manager has no
        # stdin/stdout pipes and must resume it before claiming a HOT hit.
        resident_state: ResidentState = manifest.resident_state
        if resident_state == "live":
            resident_state = "restarting" if manifest.native_session_id else "stopped"
        return _Member(
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

    @staticmethod
    def _refresh_idle_member_locked(member: _Member, manifest: SharedMemberManifest) -> None:
        """Bring an unleased, idle member up to its durable manifest.

        The caller holds the lock.
        """
        member.native_session_id = manifest.native_session_id
        member.context_status = (
            "lost"
            if member.context_status == "lost" or manifest.context_status == "lost"
            else manifest.context_status
        )
        member.activation_generation = max(
            member.activation_generation,
            manifest.activation_generation,
        )
        member.process_generation = max(
            member.process_generation,
            manifest.process_generation,
        )
        # A durable generation is not proof that the attached process was
        # launched with that generation's secret. Advance it only when there is
        # no local pipe; with a live transport, leaving the old value makes
        # ``_ensure_transport`` perform the required re-keyed native restart.
        if member.transport is None:
            member.residency_generation = max(
                member.residency_generation,
                manifest.residency_generation,
            )
        member.last_settled_job_id = manifest.last_settled_job_id
        if member.transport is None:
            member.resident_state = (
                "lost" if member.context_status == "lost" else manifest.resident_state
            )
            if member.resident_state == "live":
                member.resident_state = "restarting" if member.native_session_id else "stopped"

    def owns(self, thread_id: str) -> bool:
        """Whether this supervisor currently knows the membership alias."""
        with self._state.lock:
            return thread_id in self._state.members

    def admin_deletion_target_absent(
        self,
        thread_id: str,
        *,
        sandbox_id: str,
        container_id: str,
        on_absent: Callable[[str], None] | None = None,
    ) -> bool:
        """Prove a stale sandbox absent, optionally fencing an absence callback."""
        return prove_admin_deletion_target_absent(
            manager=self._manager,
            condition=self._state.condition,
            members=self._state.members,
            groups=self._state.groups,
            creating=self._state.creating,
            thread_id=thread_id,
            sandbox_id=sandbox_id,
            container_id=container_id,
            on_absent=on_absent,
        )


__all__ = [
    "GroupRegistration",
]
