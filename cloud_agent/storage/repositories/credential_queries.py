"""Postgres implementation of the Model-credential resolution port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

        Return the job's owner, model and MCP server scope only while its
        current attempt has a live lease and the job is running or publishing.
        The relay uses that owner to resolve stored MCP connections and checks
        the admitted server scope before forwarding a request.

        Only Agent job and attempt tables participate. Gateway account roles
        and status do not belong to this database. Inference authorization
        uses Gateway-issued grants separately from this MCP lookup.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT j.user_id, j.id AS job_id, j.model, j.state,
                       t.mcp_servers
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                JOIN agent_threads t ON t.id = j.thread_id
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
        return {
            "user_id": row["user_id"],
            "job_id": row["job_id"],
            # Read the MCP allowlist under the same fence as its owner metadata.
            "mcp_servers": list(row["mcp_servers"] or []),
            "model": row["model"],
        }
