"""Ports the control plane depends on, declared by the layer that calls them.

Each port is a :class:`typing.Protocol` naming one domain's persistence
operations. ``cloud_agent.storage`` implements them; nothing here imports
``cloud_agent.storage``, so the dependency points inward and a caller can be
handed any object with the right shape.
"""

from __future__ import annotations

from cloud_agent.ports.attachments import AttachmentRepository
from cloud_agent.ports.attempt_commands import AttemptCommands
from cloud_agent.ports.audit import AuditRepository
from cloud_agent.ports.conversation_commands import ConversationCommands
from cloud_agent.ports.conversation_queries import ConversationQueries
from cloud_agent.ports.credential_queries import CredentialQueries
from cloud_agent.ports.execution_queries import ExecutionQueries
from cloud_agent.ports.goal_wakeups import GoalWakeupRepository
from cloud_agent.ports.goals import GoalRepository
from cloud_agent.ports.grants import GrantRepository
from cloud_agent.ports.host_capacity import HostCapacityRepository
from cloud_agent.ports.host_pool import HostPoolRepository
from cloud_agent.ports.ledger import LedgerRepository
from cloud_agent.ports.mcp import McpConnectionRepository
from cloud_agent.ports.mcp_catalog import McpCatalogRepository
from cloud_agent.ports.operation_measurements import (
    MeasurementWriteResult,
    OperationMeasurements,
)
from cloud_agent.ports.profiling_control import ProfilingControl
from cloud_agent.ports.residency import ResidencyRepository
from cloud_agent.ports.sandbox import SandboxRepository
from cloud_agent.ports.session_fork import SessionForkRepository
from cloud_agent.ports.skills import SkillRepository
from cloud_agent.ports.snapshot import SnapshotRepository
from cloud_agent.ports.source_control import SourceControlRepository

__all__ = [
    "AttachmentRepository",
    "AttemptCommands",
    "AuditRepository",
    "ConversationCommands",
    "ConversationQueries",
    "CredentialQueries",
    "ExecutionQueries",
    "GoalRepository",
    "GoalWakeupRepository",
    "GrantRepository",
    "HostCapacityRepository",
    "HostPoolRepository",
    "LedgerRepository",
    "McpCatalogRepository",
    "McpConnectionRepository",
    "MeasurementWriteResult",
    "OperationMeasurements",
    "ProfilingControl",
    "ResidencyRepository",
    "SandboxRepository",
    "SessionForkRepository",
    "SkillRepository",
    "SnapshotRepository",
    "SourceControlRepository",
]
