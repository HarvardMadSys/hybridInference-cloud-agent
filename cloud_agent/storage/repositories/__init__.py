"""Postgres implementations of the capabilities the ports declare.

One module per capability. Each holds the same pool the store holds, so a
repository is a second view of one connection pool rather than a second
connection, and the transaction ownership a method had inside the store is the
transaction ownership it has here.
"""

from __future__ import annotations

from cloud_agent.storage.repositories.grants import GrantRepository
from cloud_agent.storage.repositories.mcp import McpConnectionRepository

__all__ = [
    "GrantRepository",
    "McpConnectionRepository",
]
