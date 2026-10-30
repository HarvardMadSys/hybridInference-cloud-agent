"""Port for re-encrypting what the settings root key protects outside the settings row."""

from __future__ import annotations

from typing import Protocol


class RootKeyFamilies(Protocol):
    """Values encrypted under the settings root key that live in other tables."""

    async def reseal_sandbox_archive_secret(self) -> bool:
        """Re-encrypt the archive access key under the current root key; ``False`` if none."""
        ...
