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

from cloud_agent.storage.admin_queries import AdminRunQueries
from cloud_agent.storage.admin_usage_queries import AdminUsageQueries
from cloud_agent.storage.repositories import (
    PostgresAttemptCommands,
    PostgresAuditRepository,
    PostgresConversationCommands,
    PostgresConversationQueries,
    PostgresCredentialQueries,
    PostgresExecutionQueries,
    PostgresGrantRepository,
    PostgresHostCapacityRepository,
    PostgresHostPoolRepository,
    PostgresLedgerRepository,
    PostgresMcpCatalogRepository,
    PostgresMcpConnectionRepository,
    PostgresResidencyRepository,
    PostgresSandboxRepository,
    PostgresSessionForkRepository,
    PostgresSkillRepository,
    PostgresSnapshotRepository,
    PostgresSourceControlRepository,
)

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.ports import (
        AttemptCommands,
        AuditRepository,
        ConversationCommands,
        ConversationQueries,
        CredentialQueries,
        ExecutionQueries,
        GrantRepository,
        HostCapacityRepository,
        HostPoolRepository,
        LedgerRepository,
        McpCatalogRepository,
        McpConnectionRepository,
        ResidencyRepository,
        SandboxRepository,
        SessionForkRepository,
        SkillRepository,
        SnapshotRepository,
        SourceControlRepository,
    )


class Storage:
    """Every persistence capability this service has, assembled over one pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Build one implementation per capability against the caller's pool."""
        #: Admin read models. Still concrete: they are bounded projections for
        #: one console, not a capability any other caller asks for, and they
        #: have no port yet. Named here so that the routes which need them
        #: stop reaching into another object's pool to build one.
        self.admin_runs = AdminRunQueries(pool)
        self.admin_usage = AdminUsageQueries(pool)

        self.attempt_commands: AttemptCommands = PostgresAttemptCommands(pool)
        self.audit: AuditRepository = PostgresAuditRepository(pool)
        self.conversation_commands: ConversationCommands = PostgresConversationCommands(pool)
        self.conversation_queries: ConversationQueries = PostgresConversationQueries(pool)
        self.credential_queries: CredentialQueries = PostgresCredentialQueries(pool)
        self.execution_queries: ExecutionQueries = PostgresExecutionQueries(pool)
        self.grants: GrantRepository = PostgresGrantRepository(pool)
        self.host_capacity: HostCapacityRepository = PostgresHostCapacityRepository(pool)
        self.host_pool: HostPoolRepository = PostgresHostPoolRepository(pool)
        self.ledger: LedgerRepository = PostgresLedgerRepository(pool)
        self.mcp: McpConnectionRepository = PostgresMcpConnectionRepository(pool)
        self.mcp_catalog: McpCatalogRepository = PostgresMcpCatalogRepository(pool)
        self.residency: ResidencyRepository = PostgresResidencyRepository(pool)
        self.sandbox: SandboxRepository = PostgresSandboxRepository(pool)
        self.session_fork: SessionForkRepository = PostgresSessionForkRepository(pool)
        self.skills: SkillRepository = PostgresSkillRepository(pool)
        self.snapshot: SnapshotRepository = PostgresSnapshotRepository(pool)
        self.source_control: SourceControlRepository = PostgresSourceControlRepository(pool)
