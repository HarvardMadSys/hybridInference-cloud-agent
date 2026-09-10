"""The live credential fence the MCP relay resolves per call.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class CredentialQueries(Protocol):
    """The live credential fence the MCP relay resolves per call."""

    async def resolve_model_credential(
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        """Resolve Agent-owned job metadata for the MCP relay's live fence."""
        ...
