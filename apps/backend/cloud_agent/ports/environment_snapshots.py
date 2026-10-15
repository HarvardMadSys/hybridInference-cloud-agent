"""Installation snapshot records: matching, build queueing, receipts and cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import builtins
    from collections.abc import Mapping


class EnvironmentSnapshotRepository(Protocol):
    """The snapshot queries exposed to the API and maintenance."""

    async def enabled(self) -> bool:
        """Whether this deployment stores snapshots at all."""
        ...

    async def snapshot_for_claim(
        self, *, user_id: str, binding_id: str, base_image: str | None
    ) -> dict[str, Any] | None:
        """The newest usable snapshot for a binding on a host with ``base_image``."""
        ...

    async def queue_prebuild(
        self,
        *,
        user_id: str,
        binding_id: str,
        repo: str,
        base_sha: str,
        requested: bool = False,
    ) -> str | None:
        """Queue one build of a binding's installation unless one is pending or failed lately."""
        ...

    async def capture_target(self, run: Mapping[str, Any], host: str) -> dict[str, Any] | None:
        """What a host that just claimed ``run`` should save, if anything."""
        ...

    async def record_capture(
        self, *, run_id: str, host: str, generation: int, receipt: Mapping[str, Any]
    ) -> bool:
        """Make a building snapshot ready from its own run's host receipt."""
        ...

    async def fail_capture(self, *, run_id: str, detail: str) -> None:
        """Mark the snapshot a finished run did not save as failed."""
        ...

    async def record_use(
        self, *, job_id: str, snapshot_id: str | None, outcome: str, base_sha: str | None
    ) -> bool:
        """Account for a new workspace's snapshot and queue a build when it had none."""
        ...

    async def summaries(
        self, user_id: str, fingerprints: Mapping[str, str | None]
    ) -> dict[str, dict[str, Any] | None]:
        """The latest snapshot of each environment's current installation."""
        ...

    async def discard(self, user_id: str, environment_id: str) -> int:
        """Delete an environment's snapshots."""
        ...

    async def maintain(self) -> None:
        """Expire, retire and abandon snapshots."""
        ...

    async def deletions(self, limit: int = 20) -> builtins.list[dict[str, Any]]:
        """Stored objects a host should delete, and local images to drop."""
        ...

    async def confirm_deleted(self, snapshot_id: str) -> bool:
        """Record that a host deleted a snapshot's stored object."""
        ...
