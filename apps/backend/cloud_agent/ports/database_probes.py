"""Port for the live probes that prove two connections reach one database."""

from __future__ import annotations

from typing import Protocol


class DatabaseProbes(Protocol):
    """Write a probe over this control plane's own connection and read replies back."""

    async def create_probe(self, probe_id: str, token: str) -> None:
        """Store a probe token."""
        ...

    async def probe_responses(self, probe_id: str) -> dict[str, str]:
        """Replies agents wrote over their candidate connections, by agent id."""
        ...

    async def identity(self) -> dict[str, str]:
        """What this connection says the database is (OID and system identifier)."""
        ...
