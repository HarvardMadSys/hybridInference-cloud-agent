"""Port: sandbox snapshots -- capture, publication, and base import.

Two queues with different failure modes: capturing a sandbox, and publishing
or importing the artifact that capture produced.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class SnapshotRepository(Protocol):
    """Port: sandbox snapshots -- capture, publication, and base import."""

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        """Fence one quiescent shared workspace for bounded patch capture.

        This claim never waits for publication and never changes a job state.
        The host revalidates quiescence against its live operation ledger; a
        turn that entered between this transaction and the broker call simply
        makes the capture retry later.
        """
        ...

    async def record_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        workspace_epoch: int,
        base_sha: str,
        patch: str,
        content_sha256: str,
    ) -> str | None:
        """Store one immutable broker receipt and release the capture barrier.

        Returns ``captured`` or ``published`` (for an empty no-op snapshot),
        and ``None`` for a stale/mismatched receipt.
        """
        ...

    async def defer_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        detail: str,
        dirty: bool = False,
    ) -> bool:
        """Release a failed host capture; busy retries, unsafe census dirties."""
        ...

    async def reap_stalled_sandbox_snapshot_captures(
        self, *, stall_seconds: float = 300.0
    ) -> list[int]:
        """Return abandoned CAPTURING claims to the retryable pending queue."""
        ...

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Take one immutable shared-workspace snapshot.

        Marks it ``publishing`` in the same transaction, so a second publisher
        cannot pick it up. The thread lock is shared with retained-fork
        preparation: a group snapshot may capture a succeeded boundary or a
        publisher may start mutating its lineage, but those operations cannot
        overlap. Returns the snapshot and its publication metadata, or ``None``.
        """
        ...

    async def record_snapshot_publish(
        self, *, snapshot_id: int, pr_url: str, commit_sha: str
    ) -> bool:
        """Advance one group lineage and wait for object-only base import.

        Included Agent turns stay succeeded throughout.  Publication is a
        group artifact and must never rewrite a successful model turn as a
        failure merely because GitHub or the broker was unavailable later.
        """
        ...

    async def fail_snapshot_publish(self, *, snapshot_id: int, detail: str) -> None:
        """Retain a rejected group artifact without failing included turns."""
        ...

    async def pending_snapshot_base_import(self) -> dict[str, Any] | None:
        """Return one published group commit whose object import is pending."""
        ...

    async def complete_snapshot_base_import(self, *, snapshot_id: int, commit_sha: str) -> bool:
        """Acknowledge that the exact published commit resolves in Docker."""
        ...

    async def record_snapshot_base_import_failure(self, *, snapshot_id: int, detail: str) -> None:
        """Keep an idempotent import retry visible without losing the commit."""
        ...

    async def reap_stalled_snapshot_publishes(self, *, stall_seconds: float = 900.0) -> list[int]:
        """Fail ambiguous shared publishes without changing Agent turn state."""
        ...
