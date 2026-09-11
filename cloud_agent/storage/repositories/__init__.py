"""Postgres implementations of the capabilities the ports declare.

One module per capability. Each holds the same pool the store holds, so a
repository is a second view of one connection pool rather than a second
connection, and the transaction ownership a method had inside the store is the
transaction ownership it has here.
"""

from __future__ import annotations

from cloud_agent.storage.repositories.audit import AuditRepository
from cloud_agent.storage.repositories.credential_queries import CredentialQueries
from cloud_agent.storage.repositories.execution_queries import ExecutionQueries
from cloud_agent.storage.repositories.grants import GrantRepository
from cloud_agent.storage.repositories.host_capacity import HostCapacityRepository
from cloud_agent.storage.repositories.host_pool import HostPoolRepository
from cloud_agent.storage.repositories.ledger import LedgerRepository
from cloud_agent.storage.repositories.mcp import McpConnectionRepository
from cloud_agent.storage.repositories.source_control import SourceControlRepository

__all__ = [
    "AuditRepository",
    "CredentialQueries",
    "ExecutionQueries",
    "GrantRepository",
    "HostCapacityRepository",
    "HostPoolRepository",
    "LedgerRepository",
    "McpConnectionRepository",
    "SourceControlRepository",
]
