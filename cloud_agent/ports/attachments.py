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
        """Drop a staged upload the owner removed before running."""
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
        """Read one claimed upload's bytes for the host that runs the turn."""
        ...

    async def purge_stale_staged(self, *, older_than_hours: int) -> int:
        """Collect uploads no job ever claimed."""
        ...
