"""Compute provider connections."""

from __future__ import annotations

from typing import Any

from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage.repositories.compute._common import (
    ComputeRepositoryBase,
    _json,
    _optional_row,
    _row,
)


class ConnectionsMixin(ComputeRepositoryBase):
    """Provider connections."""

    async def list_connections(self) -> list[dict[str, Any]]:
        """Return metadata only; ciphertext is restricted to trusted get_connection."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""SELECT id,name,provider_id,configuration,masked_summary,
                status,revision,observed_at,created_at,updated_at FROM agent_compute_connections
                ORDER BY created_at""")
        return [_row(row) for row in rows]

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Read the trusted connection record, including encrypted secret material."""
        async with self._pool.acquire() as conn:
            return _optional_row(
                await conn.fetchrow(
                    "SELECT * FROM agent_compute_connections WHERE id=$1", connection_id
                )
            )

    async def save_connection(
        self,
        *,
        connection_id: str,
        name: str,
        provider_id: str,
        configuration: dict[str, Any],
        secret_ciphertext: str,
        masked_summary: str,
        status: str = "unverified",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Create or compare-and-swap encrypted connection metadata."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            if expected_revision is not None and await conn.fetchval(
                """SELECT 1 FROM agent_cloudlab_autoscaling_policy p
                   WHERE p.enabled AND (p.connection_id=$1 OR p.ssh_connection_id=$1)
                   UNION ALL
                   SELECT 1 FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   JOIN agent_compute_operations o ON o.allocation_id=a.id
                   WHERE (a.connection_id=$1 OR a.ssh_connection_id=$1)
                     AND m.state<>'released'
                     AND a.external_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_compute_nodes n WHERE n.allocation_id=a.id
                     )
                     AND o.kind='acquire' AND o.state IN ('pending','running')
                   LIMIT 1""",
                connection_id,
            ):
                raise StorageConflict(ConflictCode.AUTOSCALING_CONNECTION_IN_USE)
            if expected_revision is None:
                row = await conn.fetchrow(
                    """INSERT INTO agent_compute_connections
                    (id,name,provider_id,configuration,secret_ciphertext,masked_summary,status)
                    VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7) ON CONFLICT (id) DO NOTHING RETURNING *""",
                    connection_id,
                    name,
                    provider_id,
                    _json(configuration),
                    secret_ciphertext,
                    masked_summary,
                    status,
                )
            else:
                row = await conn.fetchrow(
                    """UPDATE agent_compute_connections
                    SET name=$2,configuration=$4::jsonb,secret_ciphertext=$5,masked_summary=$6,
                        status=$7,revision=revision+1,updated_at=NOW(),
                        observed_at=CASE WHEN $7='verified' THEN NOW() ELSE observed_at END
                    WHERE id=$1 AND provider_id=$3 AND revision=$8 RETURNING *""",
                    connection_id,
                    name,
                    provider_id,
                    _json(configuration),
                    secret_ciphertext,
                    masked_summary,
                    status,
                    expected_revision,
                )
            if row is None:
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
            return _row(row)
