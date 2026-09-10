"""Observation reads over events, attempts and usage inputs.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class ExecutionQueries(Protocol):
    """Observation reads over events, attempts and usage inputs."""

    async def list_thread_events_after(
        *,
        job_id: str,
        user_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]] | None:
        """Page a whole thread's events, payloads intact."""
        ...

    async def thread_stats_inputs(
        *,
        job_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Return everything needed to summarize the thread containing a job."""
        ...

    async def job_usage_inputs(
        *,
        job_id: str,
    ) -> dict[str, Any]:
        """Read runtime usage for this job's attempts, excluding sibling turns."""
        ...

    async def job_attempts_telemetry(
        *,
        job_id: str,
    ) -> list[dict[str, Any]]:
        """Read lightweight attempt summaries without tool or process rows."""
        ...

    async def job_turn_telemetry(
        *,
        job_id: str,
        attempt_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Read one job-scoped attempt, defaulting to the newest attempt."""
        ...

    async def list_events_after(
        *,
        job_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return events for a job with a global id strictly greater than the cursor."""
        ...
