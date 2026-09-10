"""Fork preparation, leases, receipts and recovery.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class SessionForkRepository(Protocol):
    """Fork preparation, leases, receipts and recovery."""

    async def prepare_session_fork(
        *,
        source_job_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Fence a settled native boundary and reserve an invisible child."""
        ...

    async def establish_session_fork_residencies(
        *,
        operation_id: str,
        coordinator_token: str,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        """Create/recover the two independent process credentials for a fork."""
        ...

    async def claim_session_fork_materialization(
        *,
        operation_id: str,
        coordinator_token: str,
        lease_seconds: float,
    ) -> bool:
        """Lease the one external native mutation for a PREPARING saga."""
        ...

    async def release_session_fork_materialization(
        *,
        operation_id: str,
        coordinator_token: str,
    ) -> bool:
        """Release a coordinator that failed before any host mutation began."""
        ...

    async def record_session_fork_materialized(
        *,
        operation_id: str,
        coordinator_token: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a validated host receipt before exposing the child chat."""
        ...

    async def finalize_session_fork(
        *,
        operation_id: str,
    ) -> dict[str, Any] | None:
        """Atomically create the visible child history, membership and native edge."""
        ...

    async def reap_stalled_session_forks(
        *,
        stall_seconds: float = 900.0,
    ) -> list[dict[str, Any]]:
        """Recover durable native-fork receipts or fail an ambiguous attempt."""
        ...
