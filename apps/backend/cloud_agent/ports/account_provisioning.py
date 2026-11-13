"""Port: external inference provisioning rows for approved accounts."""

from __future__ import annotations

from typing import Any, Protocol


class AccountProvisioningRepository(Protocol):
    """Persist the invitation lifecycle of external inference provisioning.

    One row per account, written by the provisioning worker and the operator
    backfill command. No row holds a credential: the invitation token lives
    only in the email and at the inference service, and the issued API key
    never reaches this service.
    """

    async def ensure_activation(self) -> Any:
        """Record first activation if absent; return when provisioning began."""
        ...

    async def enqueue_new_approvals(self, *, activated_at: Any, limit: int = 50) -> int:
        """Queue accounts approved at or after activation; return how many."""
        ...

    async def enqueue_accounts(self, account_ids: list[str]) -> int:
        """Queue named eligible accounts regardless of approval time (backfill)."""
        ...

    async def eligible_account_ids(self, *, limit: int = 100000) -> list[str]:
        """Every currently eligible account id (the backfill's dry-run list)."""
        ...

    async def pending(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Invitation rows waiting to be minted and mailed, oldest first."""
        ...

    async def invited(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Rows whose mail went out, for completion polling."""
        ...

    async def access_ended(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Live rows whose account no longer has Agent use."""
        ...

    async def record_attempt(self, account_id: str, *, error: str) -> None:
        """Note a failed pass against a row; it stays queued for the next one."""
        ...

    async def mark_invited(self, account_id: str) -> None:
        """Record that the invitation mail went out, with its audit row."""
        ...

    async def mark_provisioned(self, account_id: str) -> None:
        """Record that the inference service holds a live account for this one."""
        ...

    async def mark_revoked(self, account_id: str, *, audited: bool) -> None:
        """Record that the scoped key is revoked (or was never issued)."""
        ...
