"""Postgres implementations of the capabilities the ports declare.

One module per capability. Each holds the same pool the store holds, so a
repository is a second view of one connection pool rather than a second
connection, and the transaction ownership a method had inside the store is the
transaction ownership it has here.
"""

from __future__ import annotations

from cloud_agent.storage.repositories.agent_memory import PostgresAgentMemoryRepository
from cloud_agent.storage.repositories.attachments import PostgresAttachmentRepository
from cloud_agent.storage.repositories.attempt_commands import PostgresAttemptCommands
from cloud_agent.storage.repositories.audit import PostgresAuditRepository
from cloud_agent.storage.repositories.audit_log import PostgresAuditLogRepository
from cloud_agent.storage.repositories.compute_resources import PostgresComputeResourceRepository
from cloud_agent.storage.repositories.conversation_commands import PostgresConversationCommands
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries
from cloud_agent.storage.repositories.credential_queries import PostgresCredentialQueries
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries
from cloud_agent.storage.repositories.goal_wakeups import PostgresGoalWakeupRepository
from cloud_agent.storage.repositories.goals import PostgresGoalRepository
from cloud_agent.storage.repositories.grants import PostgresGrantRepository
from cloud_agent.storage.repositories.host_capacity import PostgresHostCapacityRepository
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent.storage.repositories.ledger import PostgresLedgerRepository
from cloud_agent.storage.repositories.mcp import PostgresMcpConnectionRepository
from cloud_agent.storage.repositories.mcp_catalog import PostgresMcpCatalogRepository
from cloud_agent.storage.repositories.operation_measurements import (
    PostgresOperationMeasurementRepository,
)
from cloud_agent.storage.repositories.pending_messages import PostgresPendingMessageRepository
from cloud_agent.storage.repositories.pr_archive import PostgresPrArchiveRepository
from cloud_agent.storage.repositories.profiling_control import (
    PostgresProfilingControlRepository,
)
from cloud_agent.storage.repositories.residency import PostgresResidencyRepository
from cloud_agent.storage.repositories.sandbox import PostgresSandboxRepository
from cloud_agent.storage.repositories.session_fork import PostgresSessionForkRepository
from cloud_agent.storage.repositories.settings import PostgresSettingsRepository
from cloud_agent.storage.repositories.skills import PostgresSkillRepository
from cloud_agent.storage.repositories.snapshot import PostgresSnapshotRepository
from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository
from cloud_agent.storage.repositories.user_memory import PostgresUserMemoryRepository
from cloud_agent.storage.repositories.user_profile import PostgresUserProfileRepository
from cloud_agent.storage.repositories.web_search import PostgresWebSearchRepository

__all__ = [
    "PostgresAgentMemoryRepository",
    "PostgresAttachmentRepository",
    "PostgresAttemptCommands",
    "PostgresAuditLogRepository",
    "PostgresAuditRepository",
    "PostgresComputeResourceRepository",
    "PostgresConversationCommands",
    "PostgresConversationQueries",
    "PostgresCredentialQueries",
    "PostgresExecutionQueries",
    "PostgresGoalRepository",
    "PostgresGoalWakeupRepository",
    "PostgresGrantRepository",
    "PostgresHostCapacityRepository",
    "PostgresHostPoolRepository",
    "PostgresLedgerRepository",
    "PostgresMcpCatalogRepository",
    "PostgresMcpConnectionRepository",
    "PostgresOperationMeasurementRepository",
    "PostgresPendingMessageRepository",
    "PostgresPrArchiveRepository",
    "PostgresProfilingControlRepository",
    "PostgresResidencyRepository",
    "PostgresSandboxRepository",
    "PostgresSessionForkRepository",
    "PostgresSettingsRepository",
    "PostgresSkillRepository",
    "PostgresSnapshotRepository",
    "PostgresSourceControlRepository",
    "PostgresUserMemoryRepository",
    "PostgresUserProfileRepository",
    "PostgresWebSearchRepository",
]
