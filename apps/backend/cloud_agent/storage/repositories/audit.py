"""Postgres implementation of the admin audit trail port.

The row itself is written by
:func:`cloud_agent.storage._sql.admin_audit.insert_audit_row`, the same
statement the repositories that record inside their own transactions use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.admin_audit import insert_audit_row

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
        reason: str | None = None,
    ) -> int | None:
        """Append one row to this service's admin audit trail.

        Append-only: nothing in this service updates or deletes a row. A row
        that cannot be written is logged and counted rather than raised.

        Args:
            action: Dotted action name, e.g. ``agent.runner_host.drain``.
            actor_id: The authenticated administrator who acted.
            admin_ip: Best-effort caller address.
            target_user_id: The user affected, where there is one.
            details: Structured context. Never a credential.
            success: Whether the action succeeded.
            reason: Why, in the administrator's words, when they gave one.

        Returns:
            The new row's id, or ``None`` when it could not be written.
        """
        async with self._pool.acquire() as conn:
            return await insert_audit_row(
                conn,
                action=action,
                actor_id=actor_id,
                admin_ip=admin_ip,
                target_user_id=target_user_id,
                details=details,
                success=success,
                reason=reason,
            )
