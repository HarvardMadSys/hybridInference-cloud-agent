"""Attempt and residency grant records and revocation queries.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class GrantRepository(Protocol):
    """Attempt and residency grant records and revocation queries."""

    async def record_attempt_grant(
        *,
        attempt_id: int,
        lease_generation: int,
        grant_id: str,
    ) -> bool:
        """Attach a minted grant to the attempt it was minted for."""
        ...

    async def list_renewable_grants(
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List the grants belonging to attempts that are still current."""
        ...

    async def take_revocable_grants(
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Claim the grants of attempts that stopped being current, and clear them."""
        ...

    async def link_residency_grant(
        *,
        thread_id: str,
        generation: int,
        grant_id: str,
        binding_id: str | None = None,
    ) -> bool:
        """Record the grant a residency runs on — only its own generation."""
        ...
