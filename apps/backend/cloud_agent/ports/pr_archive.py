"""Port: durable merge checks and archival of the current published session."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class PrArchiveRepository(Protocol):
    """Claim merge checks and commit their fenced archival receipts."""

    async def claim_pr_archive_checks(
        self,
        *,
        limit: int = 50,
        lease_seconds: float = 120.0,
        access_recovery_failures: int = 0,
    ) -> list[dict[str, Any]]:
        """Claim due checks for live sessions whose latest turn was published.

        Original publication or the job's own published snapshot membership
        counts; copied fork history does not. Each result contains thread_id,
        job_id, user_id, repo, pr_url,
        claim_token and failures. Expired claims can be claimed again, and a
        completed session/PR pair is never reseeded after a manual restore.
        A check skipped because GitHub denied access is claimed again once
        due while its failures stay below ``access_recovery_failures``; 0
        never claims a skipped check.
        """
        ...

    async def reschedule_pr_archive_check(
        self,
        *,
        thread_id: str,
        pr_url: str,
        claim_token: str,
        delay_s: float,
        error_code: str | None = None,
        terminal: bool = False,
    ) -> bool:
        """Release a live claim for retry, or record a terminal skipped check.

        Errors are safe lowercase identifiers, never provider response text.
        A successful check resets consecutive failures. Expired or superseded
        claim tokens cannot change the durable obligation.
        """
        ...

    async def complete_pr_merge_archive(
        self,
        *,
        thread_id: str,
        job_id: str,
        user_id: str,
        pr_url: str,
        claim_token: str,
        merged_at: datetime,
        merge_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Archive the still-current publication and its receipt atomically.

        Recheck ownership, latest turn, publication mapping and the live claim
        under the session lock. Pause goals and pending messages while keeping
        history and shared workspaces. Return the archive receipt, or None for
        a stale claim, deleted session, waiting owner work or changed publication.
        """
        ...
