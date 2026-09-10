"""Snapshot capture, publication and base import.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class SnapshotRepository(Protocol):
    """Snapshot capture, publication and base import."""

    async def claim_due_sandbox_snapshot() -> dict[str, Any] | None:
        """Fence one quiescent shared workspace for bounded patch capture."""
        ...

    async def record_sandbox_snapshot_capture(
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
        """Store one immutable broker receipt and release the capture barrier."""
        ...

    async def defer_sandbox_snapshot_capture(
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
        *,
        stall_seconds: float = 300.0,
    ) -> list[int]:
        """Return abandoned CAPTURING claims to the retryable pending queue."""
        ...

    async def claim_for_publish() -> dict[str, Any] | None:
        """Take one immutable shared-workspace snapshot."""
        ...

    async def record_snapshot_publish(
        *,
        snapshot_id: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        """Advance one group lineage and wait for object-only base import."""
        ...

    async def fail_snapshot_publish(
        *,
        snapshot_id: int,
        detail: str,
    ) -> None:
        """Retain a rejected group artifact without failing included turns."""
        ...

    async def pending_snapshot_base_import() -> dict[str, Any] | None:
        """Return one published group commit whose object import is pending."""
        ...

    async def complete_snapshot_base_import(
        *,
        snapshot_id: int,
        commit_sha: str,
    ) -> bool:
        """Acknowledge that the exact published commit resolves in Docker."""
        ...

    async def record_snapshot_base_import_failure(
        *,
        snapshot_id: int,
        detail: str,
    ) -> None:
        """Keep an idempotent import retry visible without losing the commit."""
        ...

    async def reap_stalled_snapshot_publishes(
        *,
        stall_seconds: float = 900.0,
    ) -> list[int]:
        """Fail ambiguous shared publishes without changing Agent turn state."""
        ...
