"""Port for named, revocable administrator API credentials."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class AdminCredentialRepository(Protocol):
    """Create, list, revoke and verify administrator credentials."""

    async def create_credential(
        self,
        *,
        credential_id: str,
        name: str,
        verifier: str,
        scopes: list[str],
        created_by: str,
        expires_at: datetime | None,
    ) -> dict[str, Any]:
        """Store a new credential's verifier."""
        ...

    async def list_credentials(self) -> list[dict[str, Any]]:
        """Every credential, without its verifier."""
        ...

    async def revoke_credential(self, credential_id: str, *, revoked_by: str) -> bool:
        """Revoke a credential; it fails from the next request on."""
        ...

    async def verify_credential(self, verifier: str) -> dict[str, Any] | None:
        """The active credential with this verifier, recording its use."""
        ...
