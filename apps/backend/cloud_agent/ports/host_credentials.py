"""Port for host credential renewal and what a signing key retirement waits for."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class HostCredentials(Protocol):
    """Renewal bookkeeping on the host pool, and the tokens still signed with an old key."""

    async def issue_renewal(
        self, *, host: str, current_hash: str, pending_hash: str, key_id: str
    ) -> bool:
        """Record a renewed credential if the current one is stale or a renewal was asked for."""
        ...

    async def record_key(self, *, host: str, credential_hash: str, key_id: str) -> None:
        """Name the signing key that minted a newly enrolled host's credential."""
        ...

    async def request_renewal(self, host: str) -> bool:
        """Ask this host to switch to a fresh credential at its next heartbeat."""
        ...

    async def credential_status(self) -> list[dict[str, Any]]:
        """Every enrolled host: which key minted its credential and whether a renewal waits."""
        ...

    async def clear_previous(self) -> int:
        """Stop accepting every replaced credential."""
        ...

    async def tokens_signed_before(self, cutoff: datetime) -> dict[str, Any]:
        """Running attempts and hot residencies whose tokens were signed before ``cutoff``."""
        ...
