"""Residency lifecycles that outlive one attempt.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class ResidencyRepository(Protocol):
    """Residency lifecycles that outlive one attempt."""

    async def establish_residency(
        *,
        thread_id: str,
        host: str,
        runtime_id: str,
        runtime_version: str | None,
        ttl_seconds: float,
        sandbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Take (or take over) a thread's residency; ``None`` = not earned."""
        ...

    async def heartbeat_residency(
        *,
        thread_id: str,
        generation: int,
        ttl_seconds: float,
        host: str | None = None,
    ) -> str | None:
        """Extend a residency's lease — only the generation that holds it."""
        ...

    async def release_residency(
        *,
        thread_id: str,
        generation: int,
        host: str | None = None,
    ) -> str | None:
        """End a residency (teardown/eviction) — only its own generation."""
        ...

    async def release_shared_residency_if_quiescent(
        *,
        thread_id: str,
        generation: int,
        process_generation: int,
        host: str,
        sandbox_id: str,
    ) -> str | None:
        """Release a stopped shared member unless a durable transition owns it."""
        ...

    async def resident_host(
        *,
        thread_id: str,
    ) -> dict[str, Any] | None:
        """The live hot residency for a thread, or ``None``."""
        ...

    async def reap_expired_residencies() -> list[dict[str, Any]]:
        """Mark every hot residency whose lease lapsed as ``none``."""
        ...
