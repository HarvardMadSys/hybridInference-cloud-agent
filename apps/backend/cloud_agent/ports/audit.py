"""Port: the admin audit trail.

Admin actions are recorded through this port and read back only through
:mod:`cloud_agent.ports.audit_log`, for the admin Audit log page.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class AuditRepository(Protocol):
    """Port: the admin audit trail."""

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

        Append-only: nothing in this service updates or deletes a row, an
        erased account's included. A row that cannot be written is logged and
        counted rather than raised, so recording never fails the action.

        Args:
            action: Dotted action name, e.g. ``agent.runner_host.drain``.
            actor_id: The authenticated administrator who acted.
            admin_ip: Best-effort caller address.
            target_user_id: The user acted upon, where there is one.
            details: Structured context. Never a credential.
            success: Whether the action succeeded.
            reason: Why, in the administrator's words, when they gave one.

        Returns:
            The new row's id, or ``None`` when it could not be written.
        """
        ...
