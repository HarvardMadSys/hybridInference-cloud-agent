"""Port: the admin audit trail, read back for the Audit log page.

A port of its own rather than a second method on
:class:`cloud_agent.ports.audit.AuditRepository`: a handler that records an
action is not handed — or faked with — the ability to read every other one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime


class AuditLogQueries(Protocol):
    """Port: the admin audit trail, read back newest first."""

    async def list_admin_actions(
        self,
        *,
        actions: Sequence[str] = (),
        actor_types: Sequence[str] = (),
        subject_user_id: str | None = None,
        target: tuple[str, str] | None = None,
        before_id: int | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` audit rows, newest first.

        Newest first by ``id``, which only grows, so ``before_id`` pages
        through rows written while the page was open without repeating or
        skipping one.

        Args:
            actions: Keep only these exact actions. Empty keeps every action.
            actor_types: Keep only rows whose actor is of these kinds. Empty
                keeps every actor.
            subject_user_id: Keep only rows about this user: the account
                itself, or something it owns.
            target: Keep only rows that acted on this ``(type, id)``.
            before_id: Keep only rows older than this one, for the next page.
            limit: The most rows to return.

        Returns:
            Rows with ``id``, ``action``, ``actor_id``, ``actor_type``,
            ``actor_label``, ``admin_ip``, ``target_user_id``,
            ``subject_label``, ``target_type``, ``target_id``,
            ``target_label``, ``details`` (decoded, or ``None``), ``success``,
            ``outcome``, ``reason``, ``correlation_id``, ``created_at``, and the
            final result of a ``requested`` row as ``result`` and
            ``result_at`` (``None`` while it is still running).
        """
        ...

    async def write_failures_since(self, since: datetime) -> tuple[int, datetime | None]:
        """How many audit writes failed since ``since``, and when the latest did."""
        ...
