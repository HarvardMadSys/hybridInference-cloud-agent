"""Port: the usage ledger and the refresh queue behind it.

Rows are read for one job or one conversation; refresh work is queued,
claimed, and completed by the background refresher.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — a port's signature.
from typing import Any, Protocol


class LedgerRepository(Protocol):
    """Port: the usage ledger and the refresh queue behind it."""

    async def ledger_rows_for_job(self, *, job_id: str) -> list[dict[str, Any]]:
        """Read stored grant snapshots and retain attempts without attribution."""
        ...

    async def ledger_rows_for_conversation(
        self, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Read only this owner's conversation snapshots, including unstarted turns.

        Forks share a workspace but have distinct thread IDs and usage. Read
        cached totals in one query; a drawer must not fan out to the Gateway
        or load historical event payloads to calculate a conversation total.
        """
        ...

    async def job_usage_reads(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Everything an owner's read of a job's usage is made of, from one snapshot.

        ``job``, ``ledger_rows``, ``conversation_rows`` and ``inputs``, and the
        snapshot's ``version``: of two reads of a job's usage, the one whose
        version is greater saw everything the other did. ``None`` when the job
        is not this owner's, or its conversation was deleted.
        """
        ...

    async def queue_job_ledger_refresh(self, *, job_id: str) -> int:
        """Queue known references, sharing in-flight work and throttling retries."""
        ...

    async def next_ledger_refresh_due(self) -> datetime | None:
        """When the earliest scheduled refresh or expiring claim becomes due.

        A refresh scheduled while its row is claimed counts from the claim's
        expiry: the claiming worker reads it when that claim completes, so no
        other worker has anything to do before then. ``None`` when nothing is
        scheduled; an attempt that stops is announced by a notification instead.
        """
        ...

    async def claim_due_ledger_refresh(self, *, lease_seconds: float = 60) -> dict[str, Any] | None:
        """Fence one due snapshot query across control-plane workers.

        A crashed worker's expired lease is independently due, including
        running jobs for which there is no automatic periodic refresh.
        """
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
        """Store one fenced result without discarding the last usable snapshot.

        Terminal queries get two delayed retries for late Gateway writes.
        Failures consume retries too; an unavailable Gateway must not create
        an unbounded loop. Early manual queries update the cache but cannot
        consume a later reconciliation: stages advance by the saved query
        start time, never the response completion time. A running query which
        finishes after the attempt ends does not count as a closed-window query.
        A read requested while a running query was in flight is kept for after
        it: that query may have started before the call it was asked for.
        """
        ...
