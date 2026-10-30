"""Host credential renewal and the evidence a signing key retirement waits for.

Renewal never touches another host's row and never widens what a credential
reaches: the pending digest is this host's own renewed credential, issued only
while the request authenticated with the host's current one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


class PostgresHostCredentials:
    """Renewal bookkeeping on ``agent_runner_hosts``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool

    async def issue_renewal(
        self, *, host: str, current_hash: str, pending_hash: str, key_id: str
    ) -> bool:
        """Record a renewed credential for ``host`` if its current one is stale or a renewal was asked for.

        Only while ``current_hash`` is still the host's credential, so a late
        request cannot attach a renewal to a credential re-enrollment replaced.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_runner_hosts
                SET pending_credential_hash = $3, pending_credential_key_id = $4
                WHERE host = $1 AND credential_hash = $2 AND status <> 'disabled'
                  AND (credential_key_id IS DISTINCT FROM $4 OR credential_renewal_requested)
                  AND pending_credential_hash IS DISTINCT FROM $3
                RETURNING host
                """,
                host,
                current_hash,
                pending_hash,
                key_id,
            )
            if row is not None:
                return True
            # Already issued (the answer may have been lost): send it again.
            return bool(
                await conn.fetchval(
                    "SELECT 1 FROM agent_runner_hosts WHERE host = $1 AND credential_hash = $2 "
                    "AND pending_credential_hash = $3",
                    host,
                    current_hash,
                    pending_hash,
                )
            )

    async def record_key(self, *, host: str, credential_hash: str, key_id: str) -> None:
        """Name the signing key that minted a newly enrolled host's credential."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_runner_hosts SET credential_key_id = $3 "
                "WHERE host = $1 AND credential_hash = $2",
                host,
                credential_hash,
                key_id,
            )

    async def request_renewal(self, host: str) -> bool:
        """An administrator asks this host to switch to a fresh credential at its next heartbeat."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE agent_runner_hosts SET credential_renewal_requested = TRUE "
                "WHERE host = $1 AND credential_hash IS NOT NULL AND status <> 'disabled'",
                host,
            )
        return not status.endswith(" 0")

    async def credential_status(self) -> list[dict[str, Any]]:
        """Every enrolled host: which key minted its credential and whether a renewal waits."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host, status, credential_key_id, last_heartbeat_at,
                       pending_credential_hash IS NOT NULL AS renewal_pending,
                       credential_renewal_requested AS renewal_requested,
                       capabilities ? 'host-credential-renewal-v1' AS renews
                FROM agent_runner_hosts
                WHERE credential_hash IS NOT NULL AND status <> 'disabled'
                ORDER BY host
                """
            )
        return [dict(row) for row in rows]

    async def clear_previous(self) -> int:
        """Stop accepting every replaced credential (the signing key that minted them is retired)."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE agent_runner_hosts SET previous_credential_hash = NULL, "
                "previous_credential_expires_at = NULL WHERE previous_credential_hash IS NOT NULL"
            )
        return int(status.rsplit(" ", 1)[-1])

    async def tokens_signed_before(self, cutoff: datetime) -> dict[str, Any]:
        """Running attempts and hot residencies whose tokens were signed before ``cutoff``.

        A worker token is minted when its attempt is claimed and a residency's
        MCP credential when it is established; both stay valid only while the
        key that signed them verifies.
        """
        async with self._pool.acquire() as conn:
            attempts = await conn.fetchval(
                "SELECT count(*) FROM agent_attempts WHERE status = 'running' AND started_at < $1",
                cutoff,
            )
            residencies = await conn.fetchval(
                "SELECT count(*) FROM agent_thread_sandbox_memberships "
                "WHERE residency_status = 'hot' AND residency_established_at < $1",
                cutoff,
            )
        return {"attempts": int(attempts or 0), "residencies": int(residencies or 0)}


__all__ = ["PostgresHostCredentials"]
