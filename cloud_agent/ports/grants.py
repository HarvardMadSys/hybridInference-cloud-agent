"""Port: the inference grants an attempt runs on.

A grant is minted by the gateway and recorded here so an attempt that is
superseded can have its grants revoked. This port records and lists them;
minting and revocation are the gateway's.
"""

from __future__ import annotations

from typing import Any, Protocol


class GrantRepository(Protocol):
    """Port: the inference grants an attempt runs on."""

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        """Attach a minted grant to the attempt it was minted for."""
        ...

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """List the grants belonging to attempts that are still current."""
        ...

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Claim the grants of attempts that stopped being current, and clear them."""
        ...

    async def link_residency_grant(
        self, *, thread_id: str, generation: int, grant_id: str, binding_id: str | None = None
    ) -> bool:
        """Record the grant a residency runs on — only its own generation."""
        ...
