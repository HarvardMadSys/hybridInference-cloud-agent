"""Port: reading events, attempts, and the inputs usage is derived from.

The read half of execution. The SSE stream, the usage rollups, and the
telemetry views all draw on it; nothing here writes.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class ExecutionQueries(Protocol):
    """Port: reading events, attempts, and the inputs usage is derived from."""

    async def list_thread_events_after(
        self, *, job_id: str, user_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]] | None:
        """Page a whole thread's events, payloads intact.

        The per-job read beside this one is scoped to the turn being watched.
        Without this thread read, history keeps only durable messages, so
        everything an earlier turn *did* becomes invisible even though every
        row remains stored.

        Payloads are intentionally preserved: the caller needs tool inputs and
        outputs to reconstruct the historical activity. The result is paged so
        a long thread is not pulled into memory in one response.

        :returns: ``None`` when the job does not exist or is not the caller's,
            matching :meth:`get_thread_for_job`.
        """
        ...

    async def thread_stats_inputs(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return everything needed to summarize the thread containing a job.

        The event rows come back with their bulky text fields stripped in
        SQL: the summary reads identity, pairing and usage keys only, and a
        long thread's transcript is megabytes we would otherwise pull across
        the wire to throw away. Nothing is filtered by event type — wall
        time is measured from a turn's first to last event, so a Tier-2
        thread whose only events are ``raw`` still gets its duration.

        :returns: ``None`` when the job does not exist or is not the
            caller's, matching :meth:`get_thread_for_job`.
        """
        ...

    async def job_usage_inputs(self, *, job_id: str) -> dict[str, Any]:
        """Read runtime usage for this job's attempts, excluding sibling turns.

        Keep the same compact payload projection as thread_stats_inputs so
        output-presence detection and retry deduplication have identical
        semantics when compared with this job's Gateway snapshots.
        """
        ...

    async def job_attempts_telemetry(self, *, job_id: str) -> list[dict[str, Any]]:
        """Read lightweight attempt summaries without tool or process rows."""
        ...

    async def job_turn_telemetry(
        self, *, job_id: str, attempt_id: int | None = None
    ) -> dict[str, Any] | None:
        """Read one job-scoped attempt, defaulting to the newest attempt.

        Newest attempt rather than the current one: a retried turn's earlier
        attempts each have their own window, and the one a reader wants is the
        one that produced the result in front of them. One repeatable-read
        snapshot keeps the aggregate, per-call rows, and process rows from
        different rollup retries out of the same response. The child rows stay
        separate statements so counters arrive as numbers rather than as JSON
        this pool would hand back as text.
        """
        ...

    async def list_events_after(
        self, *, job_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return events for a job with a global id strictly greater than the cursor."""
        ...
