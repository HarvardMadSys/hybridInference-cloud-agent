"""Postgres implementation of the The admin audit trail port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresAuditRepository:
    """The admin audit trail: recorded, never read back."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

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
        """Append one row to this service's admin audit trail.

        The gateway's ``admin_audit_log`` stayed with the gateway. This one is
        ours, and it is append-only by convention rather than by constraint —
        there is no code here that updates or deletes a row.

        Args:
            action: Dotted action name, e.g. ``agent.runner_host.drain``.
            actor_id: The authenticated administrator who acted.
            admin_ip: Best-effort caller address.
            target_user_id: The user acted upon, where there is one.
            details: Structured context. Never a credential.
            success: Whether the action succeeded.

        Returns:
            The new row's id.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO agent_admin_audit_log
                    (action, actor_id, admin_ip, target_user_id, details, success)
                VALUES ($1, $6, $2, $3, $4::jsonb, $5)
                RETURNING id
                """,
                action,
                admin_ip,
                target_user_id,
                json.dumps(details) if details is not None else None,
                success,
                actor_id,
            )
