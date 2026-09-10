"""Durable admin audit records."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.storage.repositories._base import PostgresRepository


class PostgresAuditRepository(PostgresRepository):
    """Durable admin audit records."""

    async def record_admin_action(
        self,
        *,
        action: str,
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
                    (action, admin_ip, target_user_id, details, success)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                RETURNING id
                """,
                action,
                admin_ip,
                target_user_id,
                json.dumps(details) if details is not None else None,
                success,
            )
