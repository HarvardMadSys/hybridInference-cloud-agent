"""Usage snapshot reads, refresh queueing, leases and commits.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class LedgerRepository(Protocol):
    """Usage snapshot reads, refresh queueing, leases and commits."""

    async def ledger_rows_for_job(
        *,
        job_id: str,
    ) -> list[dict[str, Any]]:
        """Read stored grant snapshots and retain attempts without attribution."""
        ...

    async def ledger_rows_for_conversation(
        *,
        job_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """Read only this owner's conversation snapshots, including unstarted turns."""
        ...

    async def queue_job_ledger_refresh(
        *,
        job_id: str,
    ) -> int:
        """Queue known references, sharing in-flight work and throttling retries."""
        ...

    async def claim_due_ledger_refresh(
        *,
        lease_seconds: float = 60,
    ) -> dict[str, Any] | None:
        """Fence one due snapshot query across control-plane workers."""
        ...

    async def complete_ledger_refresh(
        *,
        ledger_id: int,
        refresh_generation: int,
        terminal: bool,
        report: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        """Store one fenced result without discarding the last usable snapshot."""
        ...
