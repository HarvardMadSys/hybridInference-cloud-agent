"""Database identity probes (``agent_database_probes``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


class PostgresDatabaseProbes:
    """Probes over the control plane's own pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind to the pool."""
        self._pool = pool

    async def create_probe(self, probe_id: str, token: str) -> None:
        """Store a probe token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_database_probes (probe_id, token) VALUES ($1, $2)",
                probe_id,
                token,
            )

    async def probe_responses(self, probe_id: str) -> dict[str, str]:
        """Replies agents wrote over their candidate connections, by agent id."""
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT responses FROM agent_database_probes WHERE probe_id = $1", probe_id
            )
        return {str(k): str(v) for k, v in (load_json(raw) or {}).items()}

    async def identity(self) -> dict[str, str]:
        """What this connection says the database is (OID and system identifier)."""
        async with self._pool.acquire() as conn:
            datid = await conn.fetchval(
                "SELECT oid::text FROM pg_database WHERE datname = current_database()"
            )
            try:
                async with conn.transaction():
                    system = await conn.fetchval(
                        "SELECT system_identifier::text FROM pg_control_system()"
                    )
            except Exception:
                system = ""
        return {"datid": str(datid or ""), "system_identifier": str(system or "")}
