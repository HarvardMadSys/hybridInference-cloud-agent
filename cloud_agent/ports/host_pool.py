"""Enrollment, heartbeat, capacity, placement and host reads.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class HostPoolRepository(Protocol):
    """Enrollment, heartbeat, capacity, placement and host reads."""

    async def issue_enroll_token(
        *,
        token_hash: str,
        host: str,
        ttl_seconds: float,
        issued_by: str | None = None,
    ) -> dict[str, Any]:
        """Record a one-time enrollment token by hash and return its expiry."""
        ...

    async def consume_enroll_token(
        *,
        token_hash: str,
        host: str,
    ) -> bool:
        """Spend an enrollment token, exactly once and only under its own name."""
        ...

    async def get_host(
        *,
        host: str,
    ) -> dict[str, Any] | None:
        """Return one host row, without its credential hash."""
        ...

    async def register_host(
        *,
        host: str,
        credential_hash: str,
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        broker_url: str | None = None,
        runner_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enrol a machine, or re-enrol one that was rebuilt."""
        ...

    async def authenticate_host(
        *,
        host: str,
        credential_hash: str,
    ) -> dict[str, Any] | None:
        """Return the host iff this credential is the one it currently holds."""
        ...

    async def record_host_heartbeat(
        *,
        host: str,
        physical_host: str | None = None,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Record a heartbeat, promoting the host to ``online`` when it may."""
        ...

    async def set_host_status(
        *,
        host: str,
        status: str,
    ) -> str:
        """Move a host to ``status``, refusing transitions the table forbids."""
        ...

    async def remove_host(
        *,
        host: str,
        force: bool = False,
    ) -> str:
        """Revoke a host's credential and take it out of the pool."""
        ...

    async def reap_offline_hosts(
        *,
        interval_seconds: float,
        missed_intervals: int,
    ) -> list[str]:
        """Mark enrolled hosts that stopped heartbeating as ``offline``."""
        ...

    async def host_for_attempt(
        *,
        attempt_id: int,
    ) -> dict[str, Any] | None:
        """The host row an attempt ran on, or ``None`` when it had none."""
        ...

    async def host_for_job(
        *,
        job_id: str,
    ) -> dict[str, Any] | None:
        """The job attempt's host, or its retained thread's durable host."""
        ...

    async def active_runner_host() -> str | None:
        """The new-chat placement host, or ``None`` when any may materialize."""
        ...

    async def runner_pool() -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
        """The pool, the pinned host and the version selection, read together."""
        ...

    async def deployment_runner_pool() -> tuple[
        list[dict[str, Any]], str | None, dict[str, str], set[str]
    ]:
        """Read placement policy, hosts, and retained owners for rollout fencing."""
        ...

    async def list_runner_hosts() -> list[dict[str, Any]]:
        """Return every host seen polling, most recent poll first."""
        ...

    async def set_runtime_version(
        *,
        runtime: str,
        version: str | None,
    ) -> bool:
        """Choose the CLI version this deployment runs for one runtime."""
        ...

    async def set_active_runner_host(
        *,
        host: str | None,
    ) -> bool:
        """Pin new-chat placement to one host, or unpin with ``None``."""
        ...

    async def forget_runner_host(
        *,
        host: str,
    ) -> str:
        """Drop a host from the pool. A runner still polling re-adds itself."""
        ...
