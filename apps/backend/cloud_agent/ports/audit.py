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
        ...
