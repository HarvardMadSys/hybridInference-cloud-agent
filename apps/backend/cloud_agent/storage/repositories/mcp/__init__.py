"""Postgres persistence for generic MCP connections and fenced invocations.

The repository is composed of one mixin per concern: connections, their
credentials and OAuth flows, their capabilities and a session's bindings,
invocations and their approvals, and transport sessions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage.repositories.mcp.capabilities import CapabilitiesMixin
from cloud_agent.storage.repositories.mcp.connections import ConnectionsMixin
from cloud_agent.storage.repositories.mcp.credentials import CredentialsMixin
from cloud_agent.storage.repositories.mcp.invocations import InvocationsMixin
from cloud_agent.storage.repositories.mcp.transport import TransportSessionsMixin

if TYPE_CHECKING:
    import asyncpg


class PostgresMcpConnectionRepository(
    ConnectionsMixin,
    CredentialsMixin,
    CapabilitiesMixin,
    InvocationsMixin,
    TransportSessionsMixin,
):
    """Per-user connections, session selections, and MCP execution state."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the control plane pool."""
        self._pool = pool
        self._allow_after: tuple[str, int] | None = None
