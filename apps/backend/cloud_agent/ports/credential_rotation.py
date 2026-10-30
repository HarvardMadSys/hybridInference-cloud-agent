"""Port for re-encrypting stored credentials and rebuilding their lookup digests."""

from __future__ import annotations

from typing import Any, Protocol


class CredentialRotation(Protocol):
    """Batches over the credential families, each write a compare-and-swap."""

    async def reencrypt_batch(
        self, family: Any, cipher: Any, *, after: str, limit: int = 200
    ) -> tuple[str | None, dict[str, int]]:
        """Rewrite up to ``limit`` rows after the cursor; ``None`` cursor when the family is done."""
        ...

    async def not_current(self, family: Any, cipher: Any) -> dict[str, int]:
        """How many stored values do not open with the current key alone."""
        ...

    async def replace_profile_secret(
        self, *, profile_id: str, expected_ciphertext: str, secret_ciphertext: str
    ) -> bool:
        """Replace a deployment profile's encrypted inputs unless they changed since read."""
        ...

    async def fingerprint_owners(self, *, after: str, limit: int) -> list[str]:
        """Owners with an environment binding or snapshot, in order after ``after``."""
        ...

    async def reindex_owner(self, user_id: str, cipher: Any) -> dict[str, int]:
        """Move one owner's environment fingerprints to the current key's digest."""
        ...
