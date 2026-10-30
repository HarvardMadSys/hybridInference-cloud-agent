"""The composition root for persistence: every capability, over one pool.

This is not a store and not a facade. It has no methods, no SQL and nothing to
delegate -- it holds one Postgres implementation per capability and names each
one, so that a caller asks for the capability it needs rather than for the
whole persistence surface.

Each attribute is annotated with its *port*, so what a caller can reach through
this object is the narrow Protocol and not the class behind it; swapping an
implementation is an edit to one line here and to nothing else.

The repositories share the pool they are handed. A capability is therefore a
second view of one connection pool, never a second pool, and the transaction
ownership of every statement is the one it had when they were all one class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage.admin_conversation_queries import AdminConversationQueries
from cloud_agent.storage.admin_database_queries import AdminDatabaseQueries
from cloud_agent.storage.admin_queries import AdminRunQueries
from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries
from cloud_agent.storage.admin_usage_queries import AdminUsageQueries
from cloud_agent.storage.repositories import (
    PostgresAgentMemoryRepository,
    PostgresAttachmentRepository,
    PostgresAttemptCommands,
    PostgresAuditLogRepository,
    PostgresAuditRepository,
    PostgresComputeResourceRepository,
    PostgresConversationCommands,
    PostgresConversationQueries,
    PostgresCredentialQueries,
    PostgresExecutionQueries,
    PostgresGoalRepository,
    PostgresGoalWakeupRepository,
    PostgresGrantRepository,
    PostgresHostCapacityRepository,
    PostgresHostPoolRepository,
    PostgresLedgerRepository,
    PostgresMcpCatalogRepository,
    PostgresMcpConnectionRepository,
    PostgresModelConnectionRepository,
    PostgresModelPreferenceRepository,
    PostgresNotificationMaintenanceRepository,
    PostgresOperationMeasurementRepository,
    PostgresOwnerUsageRepository,
    PostgresPendingMessageRepository,
    PostgresProfilingControlRepository,
    PostgresResidencyRepository,
    PostgresSandboxRepository,
    PostgresSessionForkRepository,
    PostgresSettingsRepository,
    PostgresSkillRepository,
    PostgresSnapshotRepository,
    PostgresSourceControlRepository,
    PostgresSshServerRepository,
    PostgresTeamAdminQueries,
    PostgresTeamRepository,
    PostgresUserProfileRepository,
    PostgresWebSearchRepository,
    PostgresWorkInsightsRepository,
)
from cloud_agent.storage.repositories.admin_credentials import PostgresAdminCredentialRepository
from cloud_agent.storage.repositories.database_probes import PostgresDatabaseProbes
from cloud_agent.storage.repositories.deployments import PostgresDeploymentAgentRepository
from cloud_agent.storage.repositories.environment_snapshots import (
    PostgresEnvironmentSnapshotRepository,
)
from cloud_agent.storage.repositories.environments import PostgresEnvironmentRepository
from cloud_agent.storage.repositories.instances import InstanceRepository
from cloud_agent.storage.repositories.key_rotation import PostgresRootKeyFamilies

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.ports import (
        AdminCredentialRepository,
        AgentMemoryRepository,
        AttachmentRepository,
        AttemptCommands,
        AuditLogQueries,
        AuditRepository,
        ComputeResourceRepository,
        ConversationCommands,
        ConversationQueries,
        CredentialQueries,
        DatabaseProbes,
        DeploymentAgentRepository,
        ExecutionQueries,
        GoalRepository,
        GoalWakeupRepository,
        GrantRepository,
        HostCapacityRepository,
        HostPoolRepository,
        InstanceReports,
        LedgerRepository,
        McpCatalogRepository,
        McpConnectionRepository,
        ModelConnectionRepository,
        ModelPreferenceRepository,
        NotificationMaintenanceRepository,
        OperationMeasurements,
        OwnerUsageQueries,
        PendingMessageRepository,
        ProfilingControl,
        ResidencyRepository,
        RootKeyFamilies,
        SandboxRepository,
        SessionForkRepository,
        SettingsRepository,
        SkillRepository,
        SnapshotRepository,
        SourceControlRepository,
        SshServerRepository,
        TeamAdminQueries,
        TeamRepository,
        UserProfileRepository,
        WebSearchRepository,
        WorkInsightsRepository,
    )
    from cloud_agent.ports.environment_snapshots import EnvironmentSnapshotRepository
    from cloud_agent.ports.environments import EnvironmentRepository


class Storage:
    """Every persistence capability this service has, assembled over one pool."""

    def _install_management(self, pool: asyncpg.Pool) -> None:
        """The configuration-management capabilities: instance reports, agents, credentials."""
        self.instances: InstanceReports = InstanceRepository(pool)
        self.deployments: DeploymentAgentRepository = PostgresDeploymentAgentRepository(pool)
        self.admin_credentials: AdminCredentialRepository = PostgresAdminCredentialRepository(pool)
        self.root_key_families: RootKeyFamilies = PostgresRootKeyFamilies(pool)
        self.database_probes: DatabaseProbes = PostgresDatabaseProbes(pool)

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Build one implementation per capability against the caller's pool."""
        #: Admin read models. Still concrete: they are bounded projections for
        #: one console, not a capability any other caller asks for, and they
        #: have no port yet. Named here so that the routes which need them
        #: stop reaching into another object's pool to build one.
        self.admin_conversations = AdminConversationQueries(pool)
        self._install_management(pool)
        self.admin_database = AdminDatabaseQueries(pool)
        self.admin_runs = AdminRunQueries(pool)
        self.admin_sandboxes = AdminSandboxQueries(pool)
        self.admin_usage = AdminUsageQueries(pool)

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
        self.goals: GoalRepository = PostgresGoalRepository(pool)
        self.goal_wakeups: GoalWakeupRepository = PostgresGoalWakeupRepository(pool)
        self.grants: GrantRepository = PostgresGrantRepository(pool)
        self.host_capacity: HostCapacityRepository = PostgresHostCapacityRepository(pool)
        self.host_pool: HostPoolRepository = PostgresHostPoolRepository(pool)
        self.ledger: LedgerRepository = PostgresLedgerRepository(pool)
        self.mcp: McpConnectionRepository = PostgresMcpConnectionRepository(pool)
        self.operation_measurements: OperationMeasurements = PostgresOperationMeasurementRepository(
            pool
        )
        self.mcp_catalog: McpCatalogRepository = PostgresMcpCatalogRepository(pool)
        self.model_preferences: ModelPreferenceRepository = PostgresModelPreferenceRepository(pool)
        self.notification_maintenance: NotificationMaintenanceRepository = (
            PostgresNotificationMaintenanceRepository(pool)
        )
        self.owner_usage: OwnerUsageQueries = PostgresOwnerUsageRepository(pool)
        self.pending_messages: PendingMessageRepository = PostgresPendingMessageRepository(pool)
        self.profiling_control: ProfilingControl = PostgresProfilingControlRepository(pool)
        self.residency: ResidencyRepository = PostgresResidencyRepository(pool)
        self.sandbox: SandboxRepository = PostgresSandboxRepository(pool)
        self.session_fork: SessionForkRepository = PostgresSessionForkRepository(pool)
        self.settings: SettingsRepository = PostgresSettingsRepository(pool)
        self.skills: SkillRepository = PostgresSkillRepository(pool)
        self.attachments: AttachmentRepository = PostgresAttachmentRepository(pool)
        self.snapshot: SnapshotRepository = PostgresSnapshotRepository(pool)
        self.source_control: SourceControlRepository = PostgresSourceControlRepository(pool)
        self.ssh_servers: SshServerRepository = PostgresSshServerRepository(pool)
        self.model_connections: ModelConnectionRepository = PostgresModelConnectionRepository(pool)
        self.work_insights: WorkInsightsRepository = PostgresWorkInsightsRepository(pool)
        self.agent_memory: AgentMemoryRepository = PostgresAgentMemoryRepository(pool)
        self.user_profile: UserProfileRepository = PostgresUserProfileRepository(pool)
        self.teams: TeamRepository = PostgresTeamRepository(pool)
        self.team_admin: TeamAdminQueries = PostgresTeamAdminQueries(pool)
        self.web_search: WebSearchRepository = PostgresWebSearchRepository(pool)
