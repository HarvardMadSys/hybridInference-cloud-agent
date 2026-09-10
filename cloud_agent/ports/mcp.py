"""Per-user MCP connection storage.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class McpConnectionRepository(Protocol):
    """Per-user MCP connection storage."""

    async def list_mcp_connections(
        self,
        *,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """Return this user's MCP connections, including encrypted tokens."""
        ...

    async def upsert_mcp_connection(
        self,
        *,
        user_id: str,
        server: str,
        account_label: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str | None,
        expires_at: Any,
    ) -> int:
        """Store one credential per user per server, replacing any earlier one."""
        ...

    async def delete_mcp_connection(
        self,
        *,
        user_id: str,
        server: str,
    ) -> bool:
        """Disconnect one server for this user; ``False`` if it was not connected."""
        ...
