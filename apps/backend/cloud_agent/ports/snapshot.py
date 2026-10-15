"""Port: sandbox snapshots -- capture, publication, and base import.

Two queues with different failure modes: capturing a sandbox, and publishing
or importing the artifact that capture produced.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class SnapshotRepository(Protocol):
    """Port: sandbox snapshots -- capture, publication, and base import."""

    async def snapshot_database_time(self) -> datetime:
        """Read the same clock used to fence publication retry deadlines."""
        ...

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        """Fence one quiescent shared workspace for bounded patch capture.

        This claim never waits for publication and never changes a job state.
        The host revalidates quiescence against its live operation ledger; a
        turn that entered between this transaction and the sandbox manager call simply
        makes the capture retry later.

        An earlier snapshot still on its way to publication holds the capture
        back; one that failed, and the ``dirty`` group it left, do not. Every
        capture is the whole diff from the published base, so a later one
        supersedes the failure.
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
        """Store one immutable sandbox manager receipt and release the capture barrier.

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
        """Release a failed host capture; busy retries, unsafe census dirties.

        A capture handed back for a retry is not claimed again before its
        ``retry_at``, so a busy host is asked on that clock, not in a loop.
        """
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
        Deferred publications are eligible only after their ``retry_at``.
        Epochs publish in order, past a failed one, which is never claimed
        again and which the later snapshot supersedes.
        """
        ...

    async def renew_snapshot_publish_claim(
        self, *, snapshot_id: int, expected_publish_attempt_count: int
    ) -> bool:
        """Keep a live publisher's fenced claim out of stalled recovery.

        The attempt count is the ownership generation: a publisher whose
        claim was reclaimed cannot renew the replacement's heartbeat.
        """
        ...

    async def defer_snapshot_publish(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, detail: str
    ) -> bool:
        """Spend one durable retry after a transient publication failure.

        The deterministic branch and PR are reconciled on replay, including
        after uncertain remote effects. Retains the immutable artifact and its
        included turns. No later workspace epoch publishes while this one may
        still be retried. Exhaustion fails it and dirties its group, and the
        group's next capture supersedes it. The expected attempt count fences a
        publisher whose stalled claim was already reclaimed. Returns ``False``
        unless this exact claim is still ``publishing``.
        """
        ...

    async def record_snapshot_push(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, commit_sha: str
    ) -> bool:
        """Remember the commit this claim pushed, before its pull request exists.

        A later publication cut from the same base receives it in ``own_pushes``
        and may build on it. Returns ``False`` unless this exact claim is still
        ``publishing``.
        """
        ...

    async def record_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        expected_publish_attempt_count: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        """Advance one group lineage and wait for object-only base import.

        Included Agent turns stay succeeded throughout.  Publication is a
        group artifact and must never rewrite a successful model turn as a
        failure merely because GitHub or the sandbox manager was unavailable later.
        """
        ...

    async def fail_snapshot_publish(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, detail: str
    ) -> None:
        """Retain a rejected group artifact if this publisher still owns its claim.

        It stays ``failed`` with the reason, and the group's next capture
        supersedes it.
        """
        ...

    async def pending_snapshot_base_import(self) -> dict[str, Any] | None:
        """Return one published group commit whose object import is pending."""
        ...

    async def complete_snapshot_base_import(self, *, snapshot_id: int, commit_sha: str) -> bool:
        """Acknowledge that the exact published commit resolves in Docker."""
        ...

    async def record_snapshot_base_import_failure(self, *, snapshot_id: int, detail: str) -> None:
        """Keep an idempotent import retry visible without losing the commit.

        The import is not retried before the snapshot's ``retry_at``.
        """
        ...

    async def next_snapshot_retry_due(self) -> datetime | None:
        """When the earliest capture handed back for a retry may be claimed again.

        ``None`` when none waits. Everything else that makes a capture due is
        announced by a notification.
        """
        ...

    async def next_snapshot_publish_retry_due(self) -> datetime | None:
        """When the earliest deferred publication may be claimed again.

        ``None`` when no captured snapshot waits for a future retry. Capture
        completion announces other publication work through a notification.
        """
        ...

    async def next_base_import_retry_due(self) -> datetime | None:
        """When the earliest failed base import may be retried.

        ``None`` when none waits; a workspace that starts importing is announced
        by a notification.
        """
        ...

    async def reap_stalled_snapshot_publishes(self, *, stall_seconds: float = 900.0) -> list[int]:
        """Fail ambiguous shared publishes without changing Agent turn state."""
        ...
