"""Port: what one job's agent reported about its model use.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class LedgerRepository(Protocol):
    """Port: a job's runtime usage inputs."""

    async def job_usage_reads(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Everything an owner's read of a job's usage is made of, from one snapshot.

        ``job`` and its usage ``inputs``, and the
        snapshot's ``version``: of two reads of a job's usage, the one whose
        version is greater saw everything the other did. ``None`` when the job
        is not this owner's, or its conversation was deleted.
        """
        ...
