"""The composition root for persistence: every capability, over one pool.

This is not a store and not a facade. It has no methods, no SQL and nothing to
delegate -- it holds one Postgres implementation per capability and names each
one, so that a caller asks for the capability it needs rather than for the
whole persistence surface.

Most attributes are annotated with their *port*, so what a caller can reach
through this object is the narrow Protocol and not the class behind it;
swapping an implementation is an edit to one line here and to nothing else.
An aggregate whose port only mirrored its repository has lost that port: its
attribute names the repository class, and each caller declares the narrow
Protocol it uses, which the class satisfies structurally. Callers still never
import the class.

The repositories share the pool they are handed. A capability is therefore a
second view of one connection pool, never a second pool, and the transaction
ownership of every statement is the one it had when they were all one class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage.repositories import (
    PostgresAgentMemoryRepository,
    PostgresAttachmentRepository,
    PostgresAttemptCommands,
    PostgresAuditLogRepository,
    PostgresAuditRepository,
    PostgresCommandExecutionQueries,
    PostgresComputeResourceRepository,
    PostgresContextQueries,
    PostgresConversationCommands,
    PostgresConversationQueries,
    PostgresCredentialQueries,
    PostgresExecutionQueries,
    PostgresFileActivityQueries,
    PostgresGitHubRelayRepository,
    PostgresGoalRepository,
    PostgresGoalWakeupRepository,
    PostgresHostCapacityRepository,
    PostgresHostPoolRepository,
    PostgresLedgerRepository,
    PostgresLocalAccountRepository,
    PostgresMcpCatalogRepository,
    PostgresMcpConnectionRepository,
    PostgresModelConnectionRepository,
    PostgresModelPreferenceRepository,
    PostgresNotificationMaintenanceRepository,
    PostgresOperationMeasurementRepository,
    PostgresOperatorModelRepository,
    PostgresOwnerUsageRepository,
    PostgresOwnerWorkRepository,
    PostgresPendingMessageRepository,
    PostgresProfilingControlRepository,
    PostgresResidencyRepository,
    PostgresSandboxIncidentRepository,
    PostgresSandboxRepository,
    PostgresSessionForkRepository,
    PostgresSettingsRepository,
    PostgresSkillRepository,
    PostgresSourceControlRepository,
    PostgresSshServerRepository,
    PostgresTeamAdminQueries,
    PostgresTeamRepository,
    PostgresUserProfileRepository,
    PostgresWebSearchRepository,
    PostgresWorkInsightsRepository,
)
from cloud_agent.storage.repositories.admin.conversations import (
    PostgresAdminConversationQueries,
)
from cloud_agent.storage.repositories.admin.database import PostgresAdminDatabaseQueries
from cloud_agent.storage.repositories.admin.runs import PostgresAdminRunQueries
from cloud_agent.storage.repositories.admin.sandboxes import PostgresAdminSandboxQueries
from cloud_agent.storage.repositories.admin.usage import PostgresAdminUsageQueries
from cloud_agent.storage.repositories.environment_snapshots import (
    PostgresEnvironmentSnapshotRepository,
)
from cloud_agent.storage.repositories.environments import PostgresEnvironmentRepository

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.ports import (
        AgentMemoryRepository,
        AttachmentRepository,
        AttemptCommands,
        AuditLogQueries,
        AuditRepository,
        CommandExecutionQueries,
        ComputeResourceRepository,
        ContextQueries,
        ConversationCommands,
        ConversationQueries,
        CredentialQueries,
        ExecutionQueries,
        FileActivityQueries,
        GitHubRelayRepository,
        GoalRepository,
        GoalWakeupRepository,
        HostCapacityRepository,
        HostPoolRepository,
        LedgerRepository,
        LocalAccountRepository,
        McpCatalogRepository,
        McpConnectionRepository,
        ModelConnectionRepository,
        OperationMeasurements,
        OperatorModelRepository,
        OwnerUsageQueries,
        OwnerWorkTermination,
        PendingMessageRepository,
        ProfilingControl,
        ResidencyRepository,
        SandboxRepository,
        SessionForkRepository,
        SettingsRepository,
        SkillRepository,
        SourceControlRepository,
        SshServerRepository,
        TeamAdminQueries,
        TeamRepository,
        UserProfileRepository,
        WebSearchRepository,
    )
    from cloud_agent.ports.admin_queries import (
        AdminConversationQueries,
        AdminDatabaseQueries,
        AdminRunQueries,
        AdminSandboxQueries,
        AdminUsageQueries,
    )
    from cloud_agent.ports.environment_snapshots import EnvironmentSnapshotRepository
    from cloud_agent.ports.environments import EnvironmentRepository
    from cloud_agent.ports.sandbox_incidents import SandboxIncidentRepository


class Storage:
    """Every persistence capability this service has, assembled over one pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Build one implementation per capability against the caller's pool."""
        self._admin_read_models(pool)
        self.environments: EnvironmentRepository = PostgresEnvironmentRepository(pool)
        self.environment_snapshots: EnvironmentSnapshotRepository = (
            PostgresEnvironmentSnapshotRepository(pool)
        )
        self.attempt_commands: AttemptCommands = PostgresAttemptCommands(pool)
        self.audit: AuditRepository = PostgresAuditRepository(pool)
        self.audit_log: AuditLogQueries = PostgresAuditLogRepository(pool)
        self.compute_resources: ComputeResourceRepository = PostgresComputeResourceRepository(pool)
        self.conversation_commands: ConversationCommands = PostgresConversationCommands(pool)
        self.conversation_queries: ConversationQueries = PostgresConversationQueries(pool)
        self.credential_queries: CredentialQueries = PostgresCredentialQueries(pool)
        self.execution_queries: ExecutionQueries = PostgresExecutionQueries(pool)
        self.file_activity_queries: FileActivityQueries = PostgresFileActivityQueries(pool)
        self.github_relay: GitHubRelayRepository = PostgresGitHubRelayRepository(pool)
        self.goals: GoalRepository = PostgresGoalRepository(pool)
        self.goal_wakeups: GoalWakeupRepository = PostgresGoalWakeupRepository(pool)
        self.host_capacity: HostCapacityRepository = PostgresHostCapacityRepository(pool)
        self.host_pool: HostPoolRepository = PostgresHostPoolRepository(pool)
        self.ledger: LedgerRepository = PostgresLedgerRepository(pool)
        self.mcp: McpConnectionRepository = PostgresMcpConnectionRepository(pool)
        self.operation_measurements: OperationMeasurements = PostgresOperationMeasurementRepository(
            pool
        )
        self.mcp_catalog: McpCatalogRepository = PostgresMcpCatalogRepository(pool)
        self.model_preferences: PostgresModelPreferenceRepository = (
            PostgresModelPreferenceRepository(pool)
        )
        self.notification_maintenance: PostgresNotificationMaintenanceRepository = (
            PostgresNotificationMaintenanceRepository(pool)
        )
        self.owner_usage: OwnerUsageQueries = PostgresOwnerUsageRepository(pool)
        self.context_queries: ContextQueries = PostgresContextQueries(pool)
        self.command_executions: CommandExecutionQueries = PostgresCommandExecutionQueries(pool)
        self.pending_messages: PendingMessageRepository = PostgresPendingMessageRepository(pool)
        self.profiling_control: ProfilingControl = PostgresProfilingControlRepository(pool)
        self.residency: ResidencyRepository = PostgresResidencyRepository(pool)
        self.sandbox: SandboxRepository = PostgresSandboxRepository(pool)
        self.sandbox_incidents: SandboxIncidentRepository = PostgresSandboxIncidentRepository(pool)
        self.session_fork: SessionForkRepository = PostgresSessionForkRepository(pool)
        self.settings: SettingsRepository = PostgresSettingsRepository(pool)
        self.skills: SkillRepository = PostgresSkillRepository(pool)
        self.attachments: AttachmentRepository = PostgresAttachmentRepository(pool)
        self.source_control: SourceControlRepository = PostgresSourceControlRepository(pool)
        self.ssh_servers: SshServerRepository = PostgresSshServerRepository(pool)
        self.model_connections: ModelConnectionRepository = PostgresModelConnectionRepository(pool)
        self.operator_models: OperatorModelRepository = PostgresOperatorModelRepository(pool)
        self.work_insights: PostgresWorkInsightsRepository = PostgresWorkInsightsRepository(pool)
        self.agent_memory: AgentMemoryRepository = PostgresAgentMemoryRepository(pool)
        self.user_profile: UserProfileRepository = PostgresUserProfileRepository(pool)
        self.teams: TeamRepository = PostgresTeamRepository(pool)
        self.team_admin: TeamAdminQueries = PostgresTeamAdminQueries(pool)
        self.web_search: WebSearchRepository = PostgresWebSearchRepository(pool)
        #: Accounts and the work ended with them.
        self.local_accounts: LocalAccountRepository = PostgresLocalAccountRepository(pool)
        self.owner_work: OwnerWorkTermination = PostgresOwnerWorkRepository(pool)

    def _admin_read_models(self, pool: asyncpg.Pool) -> None:
        """The admin read models.

        Bounded projections for one console, behind the ports in
        :mod:`cloud_agent.ports.admin_queries`. Named here so that the routes
        which need them stop reaching into another object's pool to build one.
        """
        self.admin_conversations: AdminConversationQueries = PostgresAdminConversationQueries(pool)
        self.admin_database: AdminDatabaseQueries = PostgresAdminDatabaseQueries(pool)
        self.admin_runs: AdminRunQueries = PostgresAdminRunQueries(pool)
        self.admin_sandboxes: AdminSandboxQueries = PostgresAdminSandboxQueries(pool)
        self.admin_usage: AdminUsageQueries = PostgresAdminUsageQueries(pool)
