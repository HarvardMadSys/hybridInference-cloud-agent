"""Claim, lease, cancellation and the atomic end of a turn.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class AttemptCommands(Protocol):
    """Claim, lease, cancellation and the atomic end of a turn."""

    async def claim_job(
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        host: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued job, creating a new fenced attempt."""
        ...

    async def release_claim(
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int | None = None,
    ) -> bool:
        """Hand a just-claimed job back to the queue without spending a retry."""
        ...

    async def heartbeat(
        *,
        attempt_id: int,
        lease_generation: int,
        lease_ttl_seconds: float,
    ) -> dict[str, Any]:
        """Extend a live lease and report cancellation, fenced by generation."""
        ...

    async def transition(
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        """Move a job between states, fenced by the attempt's live lease."""
        ...

    async def request_cancel(
        *,
        job_id: str,
        user_id: str | None = None,
    ) -> str | None:
        """Request cancellation; returns the job's resulting state."""
        ...

    async def reap_expired(
        *,
        max_attempts: int = 3,
    ) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs."""
        ...

    async def append_event(
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        """Append one event, fenced by the live lease."""
        ...

    async def record_continuity_mode(
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Stamp the diagnostic rung and optional product context semantic."""
        ...

    async def record_attempt_resources(
        *,
        attempt_id: int,
        lease_generation: int,
        rollup: dict[str, Any],
    ) -> bool:
        """Store one attempt's resource rollup, lease-fenced and idempotent."""
        ...
