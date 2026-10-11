"""Port for a user's uploaded files, independent of execution storage."""

from __future__ import annotations

from typing import Any, Protocol


class AttachmentRepository(Protocol):
    """Owner-scoped upload staging, claiming, and worker-side reads."""

    async def create_attachment(
        self, *, user_id: str, path: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """Stage one upload before any job exists, enforcing the owner's quota."""
        ...

    async def select_attachments(
        self, *, user_id: str, attachment_ids: list[int]
    ) -> list[dict[str, Any]]:
        """Read metadata for owned uploads that no job has claimed yet."""
        ...

    async def delete_attachment(self, *, user_id: str, attachment_id: int) -> bool:
        """Drop a staged upload the owner removed before running.

        Not one a waiting message holds: that message names the file and will
        be sent with it, so removing the bytes here would send a turn that
        mentions a file its sandbox never receives. Editing or deleting the
        message is what lets go of it.
        """
        ...

    async def count_thread_attachments(self, *, thread_id: str) -> int:
        """How many uploads a conversation holds, for the claim's cheap signal."""
        ...

    async def list_thread_attachments(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Every upload in a conversation, so a fresh sandbox can be rebuilt."""
        ...

    async def read_attachment_content(
        self, *, thread_id: str, attachment_id: int
    ) -> tuple[bytes, str] | None:
        """Read one claimed upload's bytes for a trusted thread-scoped caller."""
        ...

    async def list_worker_attachments(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> list[dict[str, Any]]:
        """Read metadata under the current live attempt and enrolled-host fence.

        Raise ``AttachmentLeaseLost`` if the fence is no longer authorized.
        Authorization and the upload read must share one database snapshot.
        """
        ...

    async def read_worker_attachment(
        self, *, job_id: str, attempt_id: int, lease_generation: int, attachment_id: int
    ) -> tuple[str, bytes, str] | None:
        """Read path, bytes and type under the same fence as the worker listing.

        Raise ``AttachmentLeaseLost`` for a lost fence; return ``None`` for an
        upload outside the authorized conversation, including a missing id.
        """
        ...

    async def purge_stale_staged(self, *, older_than_hours: int) -> int:
        """Collect uploads no job ever claimed and no waiting message holds.

        A message can wait behind a long turn for longer than a staged upload
        is kept. Its files are not abandoned while it waits; they are released
        back to this sweep when the message is edited to drop them or deleted.
        """
        ...
