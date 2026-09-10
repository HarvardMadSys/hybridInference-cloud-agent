"""The one place the runtime objects are assembled.

Before this module existed, ``app.py`` built an ``AgentJobStore`` and every
caller reached into it for whatever it needed. That worked while there was one
object; it stops working the moment a caller's real dependency is a capability
rather than a container, because nothing then says which capability it uses.

So the concrete implementations are constructed here, once, from one pool, and
handed to callers typed as the Protocol they need. The annotations below are the
point of the exercise: ``persistence.attempts: AttemptCommands = ...`` makes a
missing method a construction-time error rather than an ``AttributeError`` on
the one request path that happens to reach it.

Two rules this module is built to keep:

- **No business function receives :class:`Persistence`.** It has no behaviour,
  only fields, and passing it around would recreate the container this refactor
  removed. Callers take the capabilities they use.
- **The pool has one owner.** It is created by the lifespan that will close it;
  a repository only keeps a reference. Nothing here opens or closes a
  connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent.storage.repositories.attempts import PostgresAttemptCommands
from cloud_agent.storage.repositories.audit import PostgresAuditRepository
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries
from cloud_agent.storage.repositories.conversations import PostgresConversationCommands
from cloud_agent.storage.repositories.credential_queries import PostgresCredentialQueries
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries
from cloud_agent.storage.repositories.grants import PostgresGrantRepository
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent.storage.repositories.ledger import PostgresLedgerRepository
from cloud_agent.storage.repositories.mcp import PostgresMcpConnectionRepository
from cloud_agent.storage.repositories.residencies import PostgresResidencyRepository
from cloud_agent.storage.repositories.sandboxes import PostgresSandboxRepository
from cloud_agent.storage.repositories.session_forks import PostgresSessionForkRepository
from cloud_agent.storage.repositories.snapshots import PostgresSnapshotRepository
from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.ports.attempts import AttemptCommands
    from cloud_agent.ports.audit import AuditRepository
    from cloud_agent.ports.conversation_queries import ConversationQueries
    from cloud_agent.ports.conversations import ConversationCommands
    from cloud_agent.ports.credential_queries import CredentialQueries
    from cloud_agent.ports.execution_queries import ExecutionQueries
    from cloud_agent.ports.grants import GrantRepository
    from cloud_agent.ports.host_pool import HostPoolRepository
    from cloud_agent.ports.ledger import LedgerRepository
    from cloud_agent.ports.mcp import McpConnectionRepository
    from cloud_agent.ports.residencies import ResidencyRepository
    from cloud_agent.ports.sandboxes import SandboxRepository
    from cloud_agent.ports.session_forks import SessionForkRepository
    from cloud_agent.ports.snapshots import SnapshotRepository
    from cloud_agent.ports.source_control import SourceControlRepository


@dataclass(frozen=True)
class Persistence:
    """The constructed capabilities, for assembly and for tests.

    Every field is annotated with the Protocol it satisfies. Because the
    annotations are only evaluated by a type checker, the guarantee they carry is
    static: a Postgres implementation that stops matching its port fails
    ``make typecheck``, not a request. The protocols are imported under
    ``TYPE_CHECKING`` for that reason, and there is no runtime ``isinstance``
    check pretending to do the same job.
    """

    conversations: ConversationCommands
    conversation_queries: ConversationQueries
    attempts: AttemptCommands
    execution_queries: ExecutionQueries
    sandboxes: SandboxRepository
    residencies: ResidencyRepository
    session_forks: SessionForkRepository
    snapshots: SnapshotRepository
    hosts: HostPoolRepository
    source_control: SourceControlRepository
    mcp: McpConnectionRepository
    grants: GrantRepository
    credentials: CredentialQueries
    ledger: LedgerRepository
    audit: AuditRepository


def build_persistence(pool: asyncpg.Pool) -> Persistence:
    """Construct every repository over one shared pool.

    The pool is not owned here. The lifespan that created it stops the
    background tasks that use these repositories, waits for them, and only then
    closes the pool; a repository that closed it would take the others down.
    """
    return Persistence(
        conversations=PostgresConversationCommands(pool),
        conversation_queries=PostgresConversationQueries(pool),
        attempts=PostgresAttemptCommands(pool),
        execution_queries=PostgresExecutionQueries(pool),
        sandboxes=PostgresSandboxRepository(pool),
        residencies=PostgresResidencyRepository(pool),
        session_forks=PostgresSessionForkRepository(pool),
        snapshots=PostgresSnapshotRepository(pool),
        hosts=PostgresHostPoolRepository(pool),
        source_control=PostgresSourceControlRepository(pool),
        mcp=PostgresMcpConnectionRepository(pool),
        grants=PostgresGrantRepository(pool),
        credentials=PostgresCredentialQueries(pool),
        ledger=PostgresLedgerRepository(pool),
        audit=PostgresAuditRepository(pool),
    )
