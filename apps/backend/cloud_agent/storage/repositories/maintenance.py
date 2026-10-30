"""The one maintenance state every control-plane process reads (migration 0112)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


class PostgresMaintenance:
    """Begin, read and end maintenance; readable while the database is read-only."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool

    async def current(self) -> dict[str, Any] | None:
        """The maintenance row, or ``None`` when there never was one."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, kind, workflow_id, detail, started_at, updated_at "
                "FROM agent_maintenance"
            )
        return None if row is None else dict(row)

    async def begin(self, *, workflow_id: str, kind: str, detail: str) -> bool:
        """Enter maintenance for ``workflow_id``; ``False`` while another workflow holds it."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_maintenance (singleton, state, kind, workflow_id, detail)
                VALUES (TRUE, 'migrating', $2, $1, $3)
                ON CONFLICT (singleton) DO UPDATE
                SET state = 'migrating', kind = EXCLUDED.kind, workflow_id = EXCLUDED.workflow_id,
                    detail = EXCLUDED.detail,
                    started_at = CASE WHEN agent_maintenance.workflow_id = EXCLUDED.workflow_id
                                      THEN agent_maintenance.started_at ELSE NOW() END,
                    updated_at = NOW()
                WHERE agent_maintenance.state <> 'migrating'
                   OR agent_maintenance.workflow_id = EXCLUDED.workflow_id
                RETURNING workflow_id
                """,
                workflow_id,
                kind,
                detail,
            )
        return row is not None

    async def finish(self, *, workflow_id: str, state: str, detail: str) -> None:
        """Leave maintenance (``completed`` or ``aborted``) if ``workflow_id`` holds it."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_maintenance SET state = $2, detail = $3, updated_at = NOW() "
                "WHERE workflow_id = $1 AND state = 'migrating'",
                workflow_id,
                state,
                detail,
            )


__all__ = ["PostgresMaintenance"]
