"""Transport sessions a relay holds open to an MCP server."""

from __future__ import annotations

from typing import Any

from cloud_agent.storage.repositories.mcp._common import McpRepositoryBase


class TransportSessionsMixin(McpRepositoryBase):
    """Open, close and reap transport sessions."""

    async def open_mcp_transport_session(
        self,
        *,
        handle: str,
        connection_id: int,
        member: str,
        job_id: str | None,
        attempt_id: int | None,
        owner_lease_generation: int,
        protocol_version: str,
        upstream_session_id: str | None,
        config_revision: int,
        authorization_epoch: int,
    ) -> None:
        """Record one opened session under its opaque handle."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_mcp_transport_sessions
                    (handle, connection_id, member, job_id, attempt_id,
                     owner_lease_generation, protocol_version, upstream_session_id,
                     config_revision, authorization_epoch)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (handle) DO UPDATE SET
                    owner_lease_generation = EXCLUDED.owner_lease_generation,
                    protocol_version = EXCLUDED.protocol_version,
                    upstream_session_id = EXCLUDED.upstream_session_id,
                    config_revision = EXCLUDED.config_revision,
                    authorization_epoch = EXCLUDED.authorization_epoch,
                    last_used_at = NOW()
                """,
                handle,
                connection_id,
                member,
                job_id,
                attempt_id,
                owner_lease_generation,
                protocol_version,
                upstream_session_id,
                config_revision,
                authorization_epoch,
            )

    async def close_mcp_transport_session(self, *, handle: str) -> bool:
        """Forget one session. ``False`` when it was already gone."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_mcp_transport_sessions WHERE handle = $1 RETURNING handle",
                handle,
            )
        return deleted is not None

    async def reap_mcp_transport_sessions(
        self, *, connection_id: int, member: str, keep_handle: str
    ) -> list[dict[str, Any]]:
        """Delete every other session this member holds on this connection.

        The design's "explicitly rebuild the protocol session when the owner is
        superseded" is this: a takeover produces a new key, and the old session
        -- a socket the platform will never drive again -- is closed and its
        record removed rather than left for the reaper to find. Returned rather
        than counted so the caller can close the transports it still holds.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                DELETE FROM agent_mcp_transport_sessions
                WHERE connection_id = $1 AND member = $2 AND handle <> $3
                RETURNING handle, owner_lease_generation
                """,
                connection_id,
                member,
                keep_handle,
            )
        return [dict(row) for row in rows]
