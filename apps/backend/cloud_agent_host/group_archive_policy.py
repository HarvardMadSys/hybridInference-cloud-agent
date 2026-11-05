"""What a shared group's lifecycle asks of the archive that keeps its history.

The supervisor is constructed with one policy and keeps it: there is no
rebinding once groups exist. :class:`NullArchivePolicy` is the policy of a
sandbox manager without archives; :class:`ServiceArchivePolicy` and
:class:`ControllerArchivePolicy` adapt the two archive boundaries
``create_app`` builds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cloud_agent_host.chat_sandbox import SandboxRestoreFailed

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxInspection, ChatSandboxSpec
    from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
    from cloud_agent_host.sandbox_archives import SandboxArchiveService
    from cloud_agent_host.sandbox_checkpoints import CheckpointRestore
    from cloud_agent_host.shared_session_supervisor import SharedMemberManifest
    from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef


class GroupArchivePolicy(Protocol):
    """The archive transitions that bracket one group's start and stop."""

    @property
    def archives_finals(self) -> bool:
        """Whether a clean stop is archived; without it stops release their bridge at once."""
        ...

    @property
    def restores_checkpoints(self) -> bool:
        """Whether a moved group can be recreated here from its checkpoint."""
        ...

    def prepare_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Durably bind the next exact stop to the current policy, before Docker stop."""
        ...

    def stopped(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
        *,
        start_seq: int | None,
    ) -> None:
        """Archive one clean stop; ``start_seq`` labels the run the stop ended."""
        ...

    def invalidate_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """Durably skip this lifecycle's archive; ``False`` when no skip can be recorded."""
        ...

    def markerless_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """Fence a recovered markerless lifecycle; ``True`` once a marker authorizes it."""
        ...

    def prepare_start(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """Arm the start fail-stop fence; ``True`` when one was armed and must be completed."""
        ...

    def started(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Clear the start fence once the container is safely running."""
        ...

    def restore_checkpoint(self, sandbox_id: str, restore: CheckpointRestore) -> str:
        """Load a moved group's checkpoint and name its local image."""
        ...

    def environment_snapshot(self, snapshot: EnvironmentSnapshotRef) -> str | None:
        """Load an installation snapshot's image; ``None`` when snapshots are not loaded here."""
        ...

    def checkpoint_seq(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> int | None:
        """The start fence of this host's last checkpoint of exactly this container."""
        ...


@dataclass(frozen=True)
class NullArchivePolicy:
    """No archive: stops release their bridge, and nothing is restored or fenced."""

    @property
    def archives_finals(self) -> bool:
        """No final archive."""
        return False

    @property
    def restores_checkpoints(self) -> bool:
        """No checkpoints to restore."""
        return False

    def prepare_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Nothing to bind."""

    def stopped(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
        *,
        start_seq: int | None,
    ) -> None:
        """Nothing to archive."""

    def invalidate_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """No skip can be recorded."""
        return False

    def markerless_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """No authority to fence a markerless lifecycle."""
        return False

    def prepare_start(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """No start fence to arm."""
        return False

    def started(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """No start fence to clear."""

    def restore_checkpoint(self, sandbox_id: str, restore: CheckpointRestore) -> str:
        """Refuse: nothing here can restore a checkpoint."""
        raise SandboxRestoreFailed("this sandbox manager cannot restore checkpoints")

    def environment_snapshot(self, snapshot: EnvironmentSnapshotRef) -> str | None:
        """Snapshots are not loaded here."""
        return None

    def checkpoint_seq(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> int | None:
        """No checkpoint is kept here."""
        return None


@dataclass(frozen=True)
class ServiceArchivePolicy(NullArchivePolicy):
    """A static archive service: it archives clean stops and records nothing else.

    It has no authority to skip a lifecycle or fence a markerless one, so the
    supervisor keeps such groups quarantined with their bridge.
    """

    service: SandboxArchiveService

    @property
    def archives_finals(self) -> bool:
        """Clean stops are archived."""
        return True

    def stopped(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
        *,
        start_seq: int | None,
    ) -> None:
        """Archive the clean stop with the service."""
        self.service.queue_final(spec, inspection, members)


@dataclass(frozen=True)
class ControllerArchivePolicy:
    """The dynamic archive controller, which owns every marker transition and checkpoint."""

    controller: SandboxArchiveController

    @property
    def archives_finals(self) -> bool:
        """Clean stops are archived."""
        return True

    @property
    def restores_checkpoints(self) -> bool:
        """The controller restores with its current policy."""
        return True

    def prepare_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Write the pre-stop policy marker."""
        self.controller.prepare_final(spec, inspection)

    def stopped(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
        *,
        start_seq: int | None,
    ) -> None:
        """Archive the clean stop, and checkpoint it under ``start_seq``."""
        self.controller.queue_final(spec, inspection, members, start_seq=start_seq)

    def invalidate_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """Write the skipped-final marker."""
        self.controller.invalidate_final(spec, inspection)
        return True

    def markerless_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """Let the controller fence the markerless lifecycle."""
        return self.controller.prepare_markerless_final(spec, inspection)

    def prepare_start(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> bool:
        """Write the start fail-stop fence."""
        self.controller.prepare_start(spec, inspection)
        return True

    def started(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Clear the start fail-stop fence."""
        self.controller.complete_start(spec, inspection)

    def restore_checkpoint(self, sandbox_id: str, restore: CheckpointRestore) -> str:
        """Load the checkpoint image with the current policy."""
        return self.controller.restore_checkpoint(sandbox_id, restore)

    def environment_snapshot(self, snapshot: EnvironmentSnapshotRef) -> str | None:
        """Load the snapshot image with the current policy."""
        return self.controller.load_environment_snapshot(snapshot)

    def checkpoint_seq(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> int | None:
        """Read the controller's checkpoint receipts."""
        return self.controller.checkpoint_start_seq(spec, inspection)
