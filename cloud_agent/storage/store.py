"""Persistence for cloud agent-sandbox jobs (issue #1041, P0).

A small, self-contained store over the same Postgres pool used by the rest of
the gateway — kept separate from the (cached, auth-hot) ``OperationalStore``
contract, following the ``ResponseStore`` precedent.

Design (adjudicated in issue #1041):

- **State triad separation** — the sandbox/machine can vanish at any time;
  jobs, attempts, and events live here independently.
- **An expired lease is dead immediately**, not once the reaper notices. Every
  fenced write requires ``lease_expires_at > NOW()`` as well as a matching
  generation, so the window between expiry and the reaper's next pass is not a
  window in which a stalled worker can still write, renew, or finish.
- **Attempts + fencing tokens** — each grant of ownership is a new
  ``agent_attempts`` row with a monotonically increasing ``lease_generation``.
  Every write from a worker (heartbeat, events, state transitions) is
  compare-and-swapped against ``(attempt_id, lease_generation,
  status='running')`` so a zombie worker whose lease was reaped can never
  write again — the split-brain scenario is structurally impossible.
- **Reaper closes, never resets** — an expired attempt is marked
  ``superseded`` (with an ``attempt_superseded`` control event) and the job is
  requeued as a *new* attempt; history is append-only.
- **Publish is a group-snapshot side effect** — Agent turns stay terminal while
  an immutable ``agent_sandbox_snapshots`` row is claimed with ``SKIP LOCKED``.
  The sandbox never uploads a per-turn patch and never writes publish state.
- **Events** carry a global ``BIGSERIAL`` id (the SSE ``Last-Event-ID``
  cursor) plus a per-attempt ``seq`` with ``UNIQUE(attempt_id, seq)``. UI
  rewind after a retry is an ``attempt_superseded`` control event plus the new
  attempt's events — old events are never deleted or rewritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.capacity_protocol import (
    CAPACITY_HISTORY_RETENTION_S,
    CapacityHistoryPointWrite,
)
from cloud_agent.logging_utils import get_logger
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
from cloud_agent.storage.repositories.residency import PostgresResidencyRepository
from cloud_agent.storage.repositories.sandbox import PostgresSandboxRepository
from cloud_agent.storage.repositories.session_fork import PostgresSessionForkRepository
from cloud_agent.storage.repositories.snapshot import PostgresSnapshotRepository
from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    import asyncpg

logger = get_logger(__name__)


class AgentJobStore:
    """Postgres-backed store for jobs, attempts, events, and retained groups."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap a shared asyncpg pool.

        The repositories below are handed the same pool, so a migrated
        capability is a second view of one pool rather than a second
        connection.
        """
        self._pool = pool
        self._mcp = PostgresMcpConnectionRepository(pool)
        self._conversation_queries = PostgresConversationQueries(pool)
        self._execution_queries = PostgresExecutionQueries(pool)
        self._credential_queries = PostgresCredentialQueries(pool)
        self._audit = PostgresAuditRepository(pool)
        self._host_pool = PostgresHostPoolRepository(pool)
        self._source_control = PostgresSourceControlRepository(pool)
        self._ledger = PostgresLedgerRepository(pool)
        self._host_capacity = PostgresHostCapacityRepository(pool)
        self._grants = PostgresGrantRepository(pool)
        self._conversation_commands = PostgresConversationCommands(pool)
        self._attempt_commands = PostgresAttemptCommands(pool)
        self._residency = PostgresResidencyRepository(pool)
        self._snapshot = PostgresSnapshotRepository(pool)
        self._session_fork = PostgresSessionForkRepository(pool)
        self._sandbox = PostgresSandboxRepository(pool)

    async def sweep_queued_follow_ups(self) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.sweep_queued_follow_ups()

    async def initialize(self) -> None:
        """No-op: schema and backfills are Alembic's now, not startup's.

        In the source repository this method ran on every boot and did two
        jobs. It created the tables and indexes — which a migration tool
        exists to replace, since creating a table "if not exists" silently
        does nothing when the table exists with the *wrong* columns, hence the
        trail of ADD COLUMN statements after it. And it ran five **data**
        migrations: backfilling threads and thread messages for jobs that
        predate them, seeding the singleton runner-policy row, and setting
        thread_id/turn_no on legacy jobs.

        Both moved to ``migrations/versions/0001_baseline.py``, verbatim. The
        second half is easy to overlook precisely because it hid inside a
        method named for the first.

        Kept as a no-op rather than deleted so E6 can move the bootstrap call
        site unchanged.
        """
        return None

    async def record_repo_grant(
        self, *, user_id: str, installation_id: int, account_login: str | None = None
    ) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.record_repo_grant(
            user_id=user_id, installation_id=installation_id, account_login=account_login
        )

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.list_repo_grants(user_id=user_id)

    async def revoke_repo_grant(self, *, user_id: str, installation_id: int) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.revoke_repo_grant(
            user_id=user_id, installation_id=installation_id
        )

    # OAuth state is stored hashed and consumed atomically. A callback for a
    # different user/provider, an expired state, and a replay all return no
    # row and therefore cannot be distinguished or used to claim authority.

    async def create_oauth_state(
        self,
        *,
        state_hash: str,
        user_id: str,
        provider: str,
        code_verifier_ciphertext: str | None,
        expires_at: Any,
    ) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.create_oauth_state(
            state_hash=state_hash,
            user_id=user_id,
            provider=provider,
            code_verifier_ciphertext=code_verifier_ciphertext,
            expires_at=expires_at,
        )

    async def consume_oauth_state(
        self, *, state_hash: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.consume_oauth_state(
            state_hash=state_hash, user_id=user_id, provider=provider
        )

    async def upsert_gitlab_connection(
        self,
        *,
        user_id: str,
        external_user_id: int,
        username: str,
        display_name: str | None,
        web_url: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.upsert_gitlab_connection(
            user_id=user_id,
            external_user_id=external_user_id,
            username=username,
            display_name=display_name,
            web_url=web_url,
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            expires_at=expires_at,
        )

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.get_gitlab_connection(user_id=user_id)

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.update_gitlab_tokens(
            connection_id=connection_id,
            user_id=user_id,
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            expires_at=expires_at,
        )

    async def delete_gitlab_connection(self, *, user_id: str, connection_id: int) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.delete_gitlab_connection(
            user_id=user_id, connection_id=connection_id
        )

    # ── MCP connections ────────────────────────────────────────────────
    #
    # One row per user per server: what *this person* connected, holding their
    # credential. Every query here is scoped by ``user_id`` and none takes a
    # connection id alone, so there is no shape in which one user's row can be
    # read or removed through another's request — the property the deployment
    # registry could not have, because it had no user to scope by.

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return this user's MCP connections. Implemented by the repository."""
        return await self._mcp.list_mcp_connections(user_id=user_id)

    async def upsert_mcp_connection(
        self,
        *,
        user_id: str,
        server: str,
        account_label: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str | None,
        expires_at: Any,
    ) -> int:
        """Store one credential per server. Implemented by the repository."""
        return await self._mcp.upsert_mcp_connection(
            user_id=user_id,
            server=server,
            account_label=account_label,
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            expires_at=expires_at,
        )

    async def delete_mcp_connection(self, *, user_id: str, server: str) -> bool:
        """Disconnect one server. Implemented by the repository."""
        return await self._mcp.delete_mcp_connection(user_id=user_id, server=server)

    # ── Jobs ───────────────────────────────────────────────────────────

    async def create_job(
        self,
        *,
        user_id: str,
        repo: str,
        task_prompt: str,
        runtime: str,
        model: str,
        execution_profile: str | None = None,
        reasoning_effort: str | None = None,
        base_sha: str | None = None,
        setup_script: str | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_servers: list[str] | None = None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.create_job(
            user_id=user_id,
            repo=repo,
            task_prompt=task_prompt,
            runtime=runtime,
            model=model,
            execution_profile=execution_profile,
            reasoning_effort=reasoning_effort,
            base_sha=base_sha,
            setup_script=setup_script,
            metadata=metadata,
            mcp_servers=mcp_servers,
            client_request_id=client_request_id,
            client_request_fingerprint=client_request_fingerprint,
        )

    async def create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reset_reasoning_effort: bool = False,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.create_follow_up(
            parent_job_id=parent_job_id,
            user_id=user_id,
            prompt=prompt,
            runtime=runtime,
            model=model,
            reasoning_effort=reasoning_effort,
            reset_reasoning_effort=reset_reasoning_effort,
            client_request_id=client_request_id,
            client_request_fingerprint=client_request_fingerprint,
        )

    async def replayed_submission(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.replayed_submission(
            user_id=user_id,
            client_request_id=client_request_id,
            fingerprint=fingerprint,
        )

    async def prepare_session_fork(
        self,
        *,
        source_job_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.prepare_session_fork(
            source_job_id=source_job_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
        )

    async def establish_session_fork_residencies(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.establish_session_fork_residencies(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            ttl_seconds=ttl_seconds,
        )

    async def claim_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        lease_seconds: float,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.claim_session_fork_materialization(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            lease_seconds=lease_seconds,
        )

    async def release_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.release_session_fork_materialization(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
        )

    async def record_session_fork_materialized(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.record_session_fork_materialized(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            receipt=receipt,
        )

    async def finalize_session_fork(self, *, operation_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.finalize_session_fork(operation_id=operation_id)

    async def reap_stalled_session_forks(
        self, *, stall_seconds: float = 900.0
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._session_fork.reap_stalled_session_forks(stall_seconds=stall_seconds)

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_queries.get_thread_for_job(job_id=job_id, user_id=user_id)

    async def list_thread_events_after(
        self,
        *,
        job_id: str,
        user_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]] | None:
        """Delegates to the repository that implements this capability."""
        return await self._execution_queries.list_thread_events_after(
            job_id=job_id, user_id=user_id, after_id=after_id, limit=limit
        )

    async def thread_stats_inputs(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._execution_queries.thread_stats_inputs(job_id=job_id, user_id=user_id)

    async def job_usage_inputs(self, *, job_id: str) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._execution_queries.job_usage_inputs(job_id=job_id)

    async def ledger_rows_for_job(self, *, job_id: str) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.ledger_rows_for_job(job_id=job_id)

    async def ledger_rows_for_conversation(
        self, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.ledger_rows_for_conversation(job_id=job_id, user_id=user_id)

    async def queue_job_ledger_refresh(self, *, job_id: str) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.queue_job_ledger_refresh(job_id=job_id)

    async def claim_due_ledger_refresh(self, *, lease_seconds: float = 60) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.claim_due_ledger_refresh(lease_seconds=lease_seconds)

    async def complete_ledger_refresh(
        self,
        *,
        ledger_id: int,
        refresh_generation: int,
        terminal: bool,
        report: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.complete_ledger_refresh(
            ledger_id=ledger_id,
            refresh_generation=refresh_generation,
            terminal=terminal,
            report=report,
            error_code=error_code,
        )

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._credential_queries.resolve_model_credential(
            job_id=job_id, attempt_id=attempt_id, lease_generation=lease_generation
        )

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_queries.get_job(job_id=job_id)

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_queries.list_jobs(
            user_id=user_id, limit=limit, archived=archived, repo=repo
        )

    async def list_conversations(
        self,
        *,
        user_id: str,
        q: str = "",
        repo: str | None = None,
        include_archived: bool = False,
        pinned: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_queries.list_conversations(
            user_id=user_id,
            q=q,
            repo=repo,
            include_archived=include_archived,
            pinned=pinned,
            limit=limit,
            cursor=cursor,
        )

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_queries.list_projects(user_id=user_id, archived=archived)

    async def list_sessions(
        self,
        *,
        user_id: str,
        archived: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        repo: str | None = None,
        state: str | None = None,
        include_summary: bool = True,
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_queries.list_sessions(
            user_id=user_id,
            archived=archived,
            limit=limit,
            cursor=cursor,
            search=search,
            repo=repo,
            state=state,
            include_summary=include_summary,
        )

    async def set_sessions_archived(
        self, *, thread_ids: list[str], user_id: str, archived: bool
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.set_sessions_archived(
            thread_ids=thread_ids,
            user_id=user_id,
            archived=archived,
        )

    async def delete_sessions(self, *, thread_ids: list[str], user_id: str) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.delete_sessions(
            thread_ids=thread_ids,
            user_id=user_id,
        )

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.set_thread_archived(
            job_id=job_id,
            user_id=user_id,
            archived=archived,
        )

    async def set_thread_title(
        self, *, job_id: str, user_id: str, title: str
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.set_thread_title(
            job_id=job_id,
            user_id=user_id,
            title=title,
        )

    async def set_generated_thread_title(
        self, *, job_id: str, attempt_id: int, lease_generation: int, title: str
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.set_generated_thread_title(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            title=title,
        )

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._conversation_commands.set_thread_pinned(
            job_id=job_id,
            user_id=user_id,
            pinned=pinned,
        )

    # ── Runner hosts ───────────────────────────────────────────────────
    #
    # Hosts enter the pool only through one-time enrollment and authenticated
    # heartbeats. Claims pull work, but never create or rename host rows.

    async def active_runner_host(self) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.active_runner_host()

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.runner_pool()

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.deployment_runner_pool()

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.list_runner_hosts()

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.set_runtime_version(runtime=runtime, version=version)

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.set_active_runner_host(host=host)

    async def forget_runner_host(self, *, host: str) -> str:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.forget_runner_host(host=host)

    # ── Host pool: enrollment, heartbeat, capacity ─────────────────────
    #
    # The methods above are #1158's: a host appears by polling, and an operator
    # picks one. These add the half that lets a pool be *managed* — a machine
    # is enrolled with a credential, promises to heartbeat, declares how much
    # it can take and what it can run, and is drained or revoked by name.
    #
    # The two halves share one table and compose rather than compete: the pin
    # decides which host may materialize a new chat, retained groups select
    # their immutable owner, and the pool decides whether either host is in a
    # claimable state.
    #
    # Lock order everywhere below: policy row, then job rows, then host rows —
    # the order #1167 established, extended by one table. Nothing here takes a
    # host row and then reaches back for a job or the policy.

    async def issue_enroll_token(
        self, *, token_hash: str, host: str, ttl_seconds: float, issued_by: str | None = None
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.issue_enroll_token(
            token_hash=token_hash, host=host, ttl_seconds=ttl_seconds, issued_by=issued_by
        )

    async def consume_enroll_token(self, *, token_hash: str, host: str) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.consume_enroll_token(token_hash=token_hash, host=host)

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.get_host(host=host)

    async def register_host(
        self,
        *,
        host: str,
        credential_hash: str,
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        broker_url: str | None = None,
        runner_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.register_host(
            host=host,
            credential_hash=credential_hash,
            capabilities=capabilities,
            slots_total=slots_total,
            broker_url=broker_url,
            runner_version=runner_version,
            labels=labels,
        )

    async def authenticate_host(self, *, host: str, credential_hash: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.authenticate_host(host=host, credential_hash=credential_hash)

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: Mapping[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.record_host_heartbeat(
            host=host,
            credential_hash=credential_hash,
            capacity=capacity,
            capacity_sample_age_ms=capacity_sample_age_ms,
            physical_host=physical_host,
            slots_total=slots_total,
            runner_version=runner_version,
            broker_url=broker_url,
            capabilities=capabilities,
            sandbox_runtime=sandbox_runtime,
            sandbox_image_digest=sandbox_image_digest,
            runtime_versions=runtime_versions,
            runtime_versions_available=runtime_versions_available,
        )

    async def list_capacity_history_candidates(
        self, *, after_host: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.list_capacity_history_candidates(
            after_host=after_host, limit=limit
        )

    async def record_capacity_history_point(
        self,
        *,
        host: str,
        capacity_epoch: Any,
        point: CapacityHistoryPointWrite,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.record_capacity_history_point(
            host=host, capacity_epoch=capacity_epoch, point=point
        )

    async def prune_capacity_history(
        self, *, older_than_seconds: float = CAPACITY_HISTORY_RETENTION_S, limit: int = 2000
    ) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.prune_capacity_history(
            older_than_seconds=older_than_seconds, limit=limit
        )

    async def host_capacity_history(
        self, *, host: str, range_seconds: int
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.host_capacity_history(
            host=host, range_seconds=range_seconds
        )

    async def set_host_status(self, *, host: str, status: str) -> str:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.set_host_status(host=host, status=status)

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.remove_host(host=host, force=force)

    async def reap_offline_hosts(
        self,
        *,
        interval_seconds: float,
        missed_intervals: int,
    ) -> list[str]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.reap_offline_hosts(
            interval_seconds=interval_seconds, missed_intervals=missed_intervals
        )

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.host_for_attempt(attempt_id=attempt_id)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.host_for_job(job_id=job_id)

    # ── Claim / lease ──────────────────────────────────────────────────

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        host: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.claim_job(
            worker_id=worker_id,
            lease_ttl_seconds=lease_ttl_seconds,
            host=host,
            host_credential_hash=host_credential_hash,
            claim_attestation=claim_attestation,
            expected_deployment_sha=expected_deployment_sha,
        )

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.release_claim(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
        )

    async def heartbeat(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        lease_ttl_seconds: float,
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.heartbeat(
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    # ── Fenced state transitions ───────────────────────────────────────

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.transition(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            from_states=from_states,
            to_state=to_state,
            detail=detail,
            base_sha=base_sha,
        )

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.append_event(
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            event_type=event_type,
            payload=payload,
            event_seq=event_seq,
        )

    async def record_continuity_mode(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.record_continuity_mode(
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            mode=mode,
            context_status=context_status,
        )

    async def record_attempt_resources(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        rollup: dict[str, Any],
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.record_attempt_resources(
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            rollup=rollup,
        )

    async def job_attempts_telemetry(self, *, job_id: str) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._execution_queries.job_attempts_telemetry(job_id=job_id)

    async def job_turn_telemetry(
        self, *, job_id: str, attempt_id: int | None = None
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._execution_queries.job_turn_telemetry(
            job_id=job_id, attempt_id=attempt_id
        )

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.thread_workspace_ready(thread_id=thread_id)

    async def fence_terminal_workspace(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.fence_terminal_workspace(
            attempt_id=attempt_id,
            lease_generation=lease_generation,
        )

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.list_terminal_resumes_pending(limit=limit)

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.mark_terminal_resume_complete(job_id=job_id)

    async def list_events_after(
        self,
        *,
        job_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._execution_queries.list_events_after(
            job_id=job_id, after_id=after_id, limit=limit
        )

    # ── Restartable thread sandbox ─────────────────────────────────────

    async def reserve_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        host: str,
        sandbox_id: str,
        container_name: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        execution_profile: str,
        resident_slot_capacity: int = 8,
        memory_limit: str = "4g",
        cpu_limit: str = "2",
        stop_timeout_seconds: int = 10,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.reserve_thread_sandbox(
            thread_id=thread_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            host=host,
            sandbox_id=sandbox_id,
            container_name=container_name,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            sandbox_image_digest=sandbox_image_digest,
            execution_profile=execution_profile,
            resident_slot_capacity=resident_slot_capacity,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            stop_timeout_seconds=stop_timeout_seconds,
        )

    async def mark_thread_sandbox_running(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        container_id: str,
        context_status: str,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.mark_thread_sandbox_running(
            thread_id=thread_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            sandbox_generation=sandbox_generation,
            activation_generation=activation_generation,
            container_id=container_id,
            context_status=context_status,
        )

    async def record_thread_sandbox_process(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        process_generation: int,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.record_thread_sandbox_process(
            thread_id=thread_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            sandbox_generation=sandbox_generation,
            activation_generation=activation_generation,
            process_generation=process_generation,
        )

    async def record_shared_thread_residency(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_id: str,
        residency_generation: int,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.record_shared_thread_residency(
            thread_id=thread_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            sandbox_id=sandbox_id,
            residency_generation=residency_generation,
        )

    async def admit_thread_sandbox_turn(
        self,
        *,
        thread_id: str,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.admit_thread_sandbox_turn(
            thread_id=thread_id,
            job_id=job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            sandbox_generation=sandbox_generation,
            activation_generation=activation_generation,
        )

    async def settle_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        job_id: str,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.settle_thread_sandbox(
            thread_id=thread_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            sandbox_generation=sandbox_generation,
            activation_generation=activation_generation,
            job_id=job_id,
            context_status=context_status,
            native_session_id=native_session_id,
            idle_timeout_seconds=idle_timeout_seconds,
            base_sha=base_sha,
        )

    async def reconcile_thread_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None = None,
        idle_remaining_seconds: float | None = None,
        members: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.reconcile_thread_sandbox_observation(
            thread_id=thread_id,
            host=host,
            sandbox_id=sandbox_id,
            container_id=container_id,
            sandbox_generation=sandbox_generation,
            running=running,
            activity_active=activity_active,
            idle_remaining_seconds=idle_remaining_seconds,
            members=members,
        )

    async def due_thread_sandbox_stop(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.due_thread_sandbox_stop(
            thread_id=thread_id,
            host=host,
            sandbox_id=sandbox_id,
            container_id=container_id,
            sandbox_generation=sandbox_generation,
        )

    async def abandon_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        lost: bool,
        detail: str | None,
        idle_timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.abandon_thread_sandbox(
            thread_id=thread_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            sandbox_generation=sandbox_generation,
            activation_generation=activation_generation,
            lost=lost,
            detail=detail,
            idle_timeout_seconds=idle_timeout_seconds,
        )

    async def thread_sandbox(self, *, thread_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._sandbox.thread_sandbox(thread_id=thread_id)

    # ── Thread residency (design §7, issue #57) ────────────────────────
    #
    # One row per thread, never deleted, generation strictly increasing —
    # see migration 0009 for why deletion is forbidden. Every mutation is
    # generation-scoped so a late writer from a previous residency changes
    # nothing: heartbeats cannot re-promote, releases cannot end a successor.

    async def establish_residency(
        self,
        *,
        thread_id: str,
        host: str,
        runtime_id: str,
        runtime_version: str | None,
        ttl_seconds: float,
        sandbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._residency.establish_residency(
            thread_id=thread_id,
            host=host,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            ttl_seconds=ttl_seconds,
            sandbox_id=sandbox_id,
        )

    async def link_residency_grant(
        self,
        *,
        thread_id: str,
        generation: int,
        grant_id: str,
        binding_id: str | None = None,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._grants.link_residency_grant(
            thread_id=thread_id, generation=generation, grant_id=grant_id, binding_id=binding_id
        )

    async def heartbeat_residency(
        self, *, thread_id: str, generation: int, ttl_seconds: float, host: str | None = None
    ) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._residency.heartbeat_residency(
            thread_id=thread_id,
            generation=generation,
            ttl_seconds=ttl_seconds,
            host=host,
        )

    async def release_residency(
        self, *, thread_id: str, generation: int, host: str | None = None
    ) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._residency.release_residency(
            thread_id=thread_id,
            generation=generation,
            host=host,
        )

    async def release_shared_residency_if_quiescent(
        self,
        *,
        thread_id: str,
        generation: int,
        process_generation: int,
        host: str,
        sandbox_id: str,
    ) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._residency.release_shared_residency_if_quiescent(
            thread_id=thread_id,
            generation=generation,
            process_generation=process_generation,
            host=host,
            sandbox_id=sandbox_id,
        )

    async def resident_host(self, *, thread_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._residency.resident_host(thread_id=thread_id)

    async def reap_expired_residencies(self) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._residency.reap_expired_residencies()

    # ── Cancellation ───────────────────────────────────────────────────

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.request_cancel(job_id=job_id, user_id=user_id)

    # ── Shared-workspace snapshot coordination ─────────────────────────

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.claim_due_sandbox_snapshot()

    async def record_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        workspace_epoch: int,
        base_sha: str,
        patch: str,
        content_sha256: str,
    ) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.record_sandbox_snapshot_capture(
            snapshot_id=snapshot_id,
            sandbox_id=sandbox_id,
            sandbox_generation=sandbox_generation,
            container_id=container_id,
            workspace_epoch=workspace_epoch,
            base_sha=base_sha,
            patch=patch,
            content_sha256=content_sha256,
        )

    async def defer_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        detail: str,
        dirty: bool = False,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=snapshot_id,
            sandbox_id=sandbox_id,
            sandbox_generation=sandbox_generation,
            container_id=container_id,
            detail=detail,
            dirty=dirty,
        )

    async def reap_stalled_sandbox_snapshot_captures(
        self, *, stall_seconds: float = 300.0
    ) -> list[int]:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.reap_stalled_sandbox_snapshot_captures(
            stall_seconds=stall_seconds,
        )

    # ── Platform-side publishing ───────────────────────────────────────
    #
    # Publishing is driven by the trusted control plane, never by the Agent
    # worker. The host captures a fenced, immutable group snapshot; no sandbox
    # credential can upload or replace it. Snapshot state plus ``SKIP LOCKED``
    # provides exactly-once claiming across control-plane replicas.

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.claim_for_publish()

    async def record_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.record_snapshot_publish(
            snapshot_id=snapshot_id,
            pr_url=pr_url,
            commit_sha=commit_sha,
        )

    async def fail_snapshot_publish(self, *, snapshot_id: int, detail: str) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.fail_snapshot_publish(snapshot_id=snapshot_id, detail=detail)

    async def pending_snapshot_base_import(self) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.pending_snapshot_base_import()

    async def complete_snapshot_base_import(self, *, snapshot_id: int, commit_sha: str) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.complete_snapshot_base_import(
            snapshot_id=snapshot_id,
            commit_sha=commit_sha,
        )

    async def record_snapshot_base_import_failure(self, *, snapshot_id: int, detail: str) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.record_snapshot_base_import_failure(
            snapshot_id=snapshot_id,
            detail=detail,
        )

    # ── Reaper ─────────────────────────────────────────────────────────

    async def reap_stalled_snapshot_publishes(self, *, stall_seconds: float = 900.0) -> list[int]:
        """Delegates to the repository that implements this capability."""
        return await self._snapshot.reap_stalled_snapshot_publishes(stall_seconds=stall_seconds)

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._attempt_commands.reap_expired(max_attempts=max_attempts)

    # ── Inference grants (E9) ──────────────────────────────────────────
    #
    # The grant itself lives at the gateway; what lives here is the *link*
    # between an attempt and the capability minted for it, and that link is the
    # fence. After the split the gateway holds no attempt or lease state at
    # all, so nothing there can notice that an attempt was superseded — this
    # store is the only place that knows, which is why renewal reads from here
    # rather than from a timer.

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._grants.record_attempt_grant(
            attempt_id=attempt_id, lease_generation=lease_generation, grant_id=grant_id
        )

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._grants.list_renewable_grants(limit=limit)

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._grants.take_revocable_grants(limit=limit)

    # ── Admin audit trail (E7) ─────────────────────────────────────────

    async def record_admin_action(
        self,
        *,
        action: str,
        actor_id: str,
        admin_ip: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._audit.record_admin_action(
            action=action,
            actor_id=actor_id,
            admin_ip=admin_ip,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )
