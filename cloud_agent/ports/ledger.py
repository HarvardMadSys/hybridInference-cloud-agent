"""Port: the per-attempt inference ledger.

Usage snapshots the gateway reports for one attempt, plus the refresh queue
that reconciles a job's totals after its turns settle.
"""

from __future__ import annotations

from typing import Any, Protocol


class LedgerRepository(Protocol):
    """Port: the per-attempt inference ledger."""

    async def ledger_rows_for_job(self, *, job_id: str) -> list[dict[str, Any]]:
        """Read stored grant snapshots and retain attempts without attribution."""
        ...

    async def ledger_rows_for_conversation(
        self, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Read only this owner's conversation snapshots, including unstarted turns."""
        ...

    async def queue_job_ledger_refresh(self, *, job_id: str) -> int:
        """Queue known references, sharing in-flight work and throttling retries."""
        ...

    async def claim_due_ledger_refresh(self, *, lease_seconds: float = 60) -> dict[str, Any] | None:
        """Fence one due snapshot query across control-plane workers."""
        ...

    async def complete_ledger_refresh(
        self,
        *,
        ledger_id: int,
        refresh_generation: int,
        terminal: bool,
        report: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        """Store one fenced result without discarding the last usable snapshot."""
        ...
