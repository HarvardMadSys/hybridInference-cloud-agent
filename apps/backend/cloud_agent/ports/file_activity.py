"""Owner-scoped reads behind a conversation's File Activity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cloud_agent.file_activity.models import (
        FileActivityView,
        FileOperationsPage,
    )


class FileActivityQueries(Protocol):
    """Read one conversation's recorded file operations, on demand and bounded."""

    async def file_activity(
        self,
        *,
        job_id: str,
        user_id: str,
        sort: str,
        filter_key: str,
        q: str,
        cursor: str | None,
        limit: int,
    ) -> FileActivityView | None:
        """One page of the caller's conversation, or None when it is not theirs."""
        ...

    async def file_operations(
        self,
        *,
        job_id: str,
        user_id: str,
        path: str,
        cursor: str | None,
        limit: int,
    ) -> FileOperationsPage | None:
        """One page of one file's recorded operations, or None when not theirs."""
        ...
