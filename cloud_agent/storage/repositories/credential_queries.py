"""Postgres implementation of the Model-credential resolution port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.mcp import mcp_binding_row

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresCredentialQueries:
    """Resolving the model credential an attempt runs with."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        """Resolve Agent-owned job metadata for the MCP relay's live fence.

        Return the job's owner, model and MCP binding scope only while its
        current attempt has a live lease and the job is running or publishing.
        The relay uses that owner to resolve stored MCP connections and checks
        the admitted binding before forwarding a request.

        Only Agent job and attempt tables participate. Gateway account roles
        and status do not belong to this database. Inference authorization
        uses Gateway-issued grants separately from this MCP lookup.

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
                """
                SELECT j.user_id, j.id AS job_id, j.model, j.state, j.thread_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1
                  AND a.lease_generation = $2
                  AND a.status = 'running'
                  AND a.lease_expires_at > NOW()
                  AND j.id = $3
                  AND j.current_attempt_id = a.id
                  AND j.state = 'running'
                """,
                attempt_id,
                lease_generation,
                job_id,
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
