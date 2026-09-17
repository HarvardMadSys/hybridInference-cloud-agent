"""Postgres implementations of the capabilities the ports declare.

One module per capability. Each holds the same pool the store holds, so a
repository is a second view of one connection pool rather than a second
connection, and the transaction ownership a method had inside the store is the
transaction ownership it has here.
"""

from __future__ import annotations

from cloud_agent.storage.repositories.attempt_commands import PostgresAttemptCommands
from cloud_agent.storage.repositories.audit import PostgresAuditRepository
from cloud_agent.storage.repositories.conversation_commands import PostgresConversationCommands
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries
from cloud_agent.storage.repositories.credential_queries import PostgresCredentialQueries
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries
from cloud_agent.storage.repositories.grants import PostgresGrantRepository
from cloud_agent.storage.repositories.host_capacity import PostgresHostCapacityRepository
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent.storage.repositories.ledger import PostgresLedgerRepository
from cloud_agent.storage.repositories.mcp import PostgresMcpConnectionRepository
from cloud_agent.storage.repositories.mcp_catalog import PostgresMcpCatalogRepository
from cloud_agent.storage.repositories.residency import PostgresResidencyRepository
from cloud_agent.storage.repositories.sandbox import PostgresSandboxRepository
from cloud_agent.storage.repositories.session_fork import PostgresSessionForkRepository
from cloud_agent.storage.repositories.skills import PostgresSkillRepository
from cloud_agent.storage.repositories.snapshot import PostgresSnapshotRepository
from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository

__all__ = [
    "PostgresAttemptCommands",
    "PostgresAuditRepository",
    "PostgresConversationCommands",
    "PostgresConversationQueries",
    "PostgresCredentialQueries",
    "PostgresExecutionQueries",
    "PostgresGrantRepository",
    "PostgresHostCapacityRepository",
    "PostgresHostPoolRepository",
    "PostgresLedgerRepository",
    "PostgresMcpCatalogRepository",
    "PostgresMcpConnectionRepository",
    "PostgresResidencyRepository",
    "PostgresSandboxRepository",
    "PostgresSessionForkRepository",
    "PostgresSkillRepository",
    "PostgresSnapshotRepository",
    "PostgresSourceControlRepository",
]
