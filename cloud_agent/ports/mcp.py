"""Port: a user's own MCP server connections.

One connection per reviewed catalogue entry, kept per user and never
shared between them.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class McpConnectionRepository(Protocol):
    """Port: a user's own MCP server connections."""

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return this user's MCP connections, including encrypted tokens.

        Ordered by server name so the composer and the Integrations page agree
        on order without either sorting again.
        """
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
        """Store one credential per user per server, replacing any earlier one.

        Reconnecting is an update rather than a second row: "connected" is a
        yes/no per person per server, and two live credentials for one pair
        would leave which one a job used down to row order.
        """
        ...

    async def delete_mcp_connection(self, *, user_id: str, server: str) -> bool:
        """Disconnect one server for this user; ``False`` if it was not connected."""
        ...
