"""Administrator API credentials (``agent_admin_credentials``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

_COLUMNS = (
    "credential_id, name, scopes, created_by, created_at, expires_at, revoked_at, revoked_by, "
    "last_used_at"
)


class PostgresAdminCredentialRepository:
    """The credential capability over the control plane's pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind to the pool."""
        self._pool = pool

    async def create_credential(
        self,
        *,
        credential_id: str,
        name: str,
        verifier: str,
        scopes: list[str],
        created_by: str,
        expires_at: datetime | None,
    ) -> dict[str, Any]:
        """Store a new credential's verifier."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_admin_credentials
                    (credential_id, name, verifier, scopes, created_by, expires_at)
                VALUES ($1, $2, $3, $4::text[], $5, $6)
                RETURNING {_COLUMNS}
                """,
                credential_id,
                name,
                verifier,
                sorted(set(scopes)),
                created_by,
                expires_at,
            )
        return dict(row)

    async def list_credentials(self) -> list[dict[str, Any]]:
        """Every credential, without its verifier."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM agent_admin_credentials "
                "ORDER BY revoked_at IS NOT NULL, created_at DESC"
            )
        return [dict(row) for row in rows]

    async def revoke_credential(self, credential_id: str, *, revoked_by: str) -> bool:
        """Revoke a credential; it fails from the next request on."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE agent_admin_credentials SET revoked_at = NOW(), revoked_by = $2 "
                "WHERE credential_id = $1 AND revoked_at IS NULL",
                credential_id,
                revoked_by,
            )
        return status.endswith(" 1")

    async def verify_credential(self, verifier: str) -> dict[str, Any] | None:
        """The active credential with this verifier, recording its use (at most once a minute)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_admin_credentials
                SET last_used_at = CASE
                    WHEN last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '1 minute'
                    THEN NOW() ELSE last_used_at END
                WHERE verifier = $1 AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > NOW())
                RETURNING {_COLUMNS}
                """,
                verifier,
            )
        return None if row is None else dict(row)
