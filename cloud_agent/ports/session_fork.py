"""Port: forking a native session into a sibling sandbox member.

A fork is a multi-step operation with its own claim, materialization record,
and failure cleanup; each step is exposed so the caller can be resumed at any
point without forking twice.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class SessionForkRepository(Protocol):
    """Port: forking a native session into a sibling sandbox member."""

    async def prepare_session_fork(
        self, *, source_job_id: str, user_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """Fence a settled native boundary and reserve an invisible child."""
        ...

    async def establish_session_fork_residencies(
        self, *, operation_id: str, coordinator_token: str, ttl_seconds: float
    ) -> dict[str, Any] | None:
        """Create/recover the two independent process credentials for a fork."""
        ...

    async def claim_session_fork_materialization(
        self, *, operation_id: str, coordinator_token: str, lease_seconds: float
    ) -> bool:
        """Lease the one external native mutation for a PREPARING saga.

        Idempotency-key insertion is serialized in :meth:`prepare_session_fork`,
        but its HTTP handler necessarily leaves that transaction before minting
        credentials and calling the host. This second durable fence prevents
        two workers handling the same request from rotating both member
        residencies and invoking the native fork concurrently. An abandoned
        coordinator can be taken over after the bounded lease.
        """
        ...

    async def release_session_fork_materialization(
        self, *, operation_id: str, coordinator_token: str
    ) -> bool:
        """Release a coordinator that failed before any host mutation began."""
        ...

    async def record_session_fork_materialized(
        self, *, operation_id: str, coordinator_token: str, receipt: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Persist a validated host receipt before exposing the child chat."""
        ...

    async def finalize_session_fork(self, *, operation_id: str) -> dict[str, Any] | None:
        """Atomically create the visible child history, membership and native edge."""
        ...

    async def reap_stalled_session_forks(
        self, *, stall_seconds: float = 900.0
    ) -> list[dict[str, Any]]:
        """Recover durable native-fork receipts or fail an ambiguous attempt.

        ``materialized`` has a validated host receipt and is safe to finalize.
        A stale ``preparing`` operation has no receipt the control plane can
        trust, so it is failed without exposing its hidden target. Its exact
        source/target residency generations are lapsed for member-scoped host
        cleanup and grant revocation; siblings are never touched.
        """
        ...
