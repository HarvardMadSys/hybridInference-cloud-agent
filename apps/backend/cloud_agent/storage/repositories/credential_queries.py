"""Postgres implementation of the Model-credential resolution port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST
from cloud_agent.storage._sql.mcp import mcp_binding_row
from cloud_agent.storage._sql.mcp_residency import residency_fence_sql

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresCredentialQueries:
    """Resolving the model credential an attempt runs with."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def resolve_residency_attempt(
        self, *, thread_id: str, residency_generation: int
    ) -> dict[str, Any] | None:
        """Name the attempt a resident process is serving right now.

        The membership is the residency's record: its generation and lease say
        whether the process that holds the credential is still the chat's, and
        its ``active_attempt_id`` is the turn admitted to it. That attempt must
        be its job's current one and run on the membership's own host, so a
        credential copied out of one sandbox cannot speak for a turn the chat
        runs anywhere else.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT j.id AS job_id, a.id AS attempt_id, a.lease_generation
                FROM agent_thread_sandbox_memberships m
                JOIN agent_threads t ON t.id = m.thread_id
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                JOIN agent_attempts a ON a.id = m.active_attempt_id
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE m.thread_id = $1
                  AND m.residency_generation = $2
                  AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND t.deleted_at IS NULL
                  AND a.host_id = g.host
                  AND a.status = 'running'
                  AND j.thread_id = m.thread_id
                  AND j.current_attempt_id = a.id
                """,
                thread_id,
                residency_generation,
            )
        return dict(row) if row is not None else None

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        residency_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Resolve Agent-owned job metadata for the MCP relay's live fence.

        Return the job's owner, model and MCP binding scope only while its
        current attempt has a live lease, its job is running and has not been
        asked to stop, and the machine it runs on is still enrolled. The relay
        uses that owner to resolve stored MCP connections and checks the
        admitted binding before forwarding a request.

        The last two are what make Stop and host removal take effect here.
        Neither changes the attempt's lease or the job's state at once — a
        stopped turn settles on the lease it holds, and a removed machine's
        worker tokens still verify — so without them the relay would keep
        serving tools to a turn its owner had stopped, or to a machine that
        was taken out of the pool, until the lease ran out.

        Only Agent job, attempt and host tables participate. Gateway account
        roles and status do not belong to this database. Inference
        authorization uses Gateway-issued grants separately from this MCP
        lookup.

        The bindings are read fresh on every call rather than carried on the
        job, so narrowing a session takes effect on its next tool call instead
        of at its next turn.

        Disabled bindings are filtered out here rather than refused later. A
        binding turned off with no capabilities selected would otherwise be
        handed to the runner as a server whose every call fails, and the model
        would spend its budget discovering that one tool at a time instead of
        being offered nothing at all.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT j.user_id, j.id AS job_id, j.model, j.state, j.thread_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1
                  AND a.lease_generation = $2
                  AND a.status = 'running'
                  AND a.lease_expires_at > clock_timestamp()
                  AND j.id = $3
                  AND j.current_attempt_id = a.id
                  AND j.state = 'running'
                  AND NOT j.cancel_requested
                  AND {LIVE_ATTEMPT_HOST}
                  AND {residency_fence_sql("$4")}
                """,
                attempt_id,
                lease_generation,
                job_id,
                residency_generation,
            )
            if row is None:
                return None
            bindings = await conn.fetch(
                """
                SELECT b.thread_id, b.connection_id, b.alias, b.enabled, b.selection_pending,
                       b.config_revision, b.authorization_epoch, b.capability_revision,
                       b.policy_revision, b.rules, b.resource_rules, b.prompt_rules,
                       c.user_id AS owner_id, c.status AS connection_status,
                       c.deleted_at, c.label, c.transport, c.location
                FROM agent_mcp_thread_bindings b
                JOIN agent_mcp_connections c ON c.id = b.connection_id
                WHERE b.thread_id = $1 AND b.enabled
                ORDER BY b.alias, b.connection_id
                """,
                row["thread_id"],
            )
        return {
            "user_id": row["user_id"],
            "job_id": row["job_id"],
            "thread_id": row["thread_id"],
            # Read under the same fence as the owner metadata, and never
            # returned to the sandbox: the relay is the only reader.
            "mcp_bindings": [mcp_binding_row(binding) for binding in bindings],
            "model": row["model"],
        }
