"""Port: the admin audit trail, read back for the Audit log page.

A port of its own rather than a second method on
:class:`cloud_agent.ports.audit.AuditRepository`: a handler that records an
action is not handed — or faked with — the ability to read every other one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class AuditLogQueries(Protocol):
    """Port: the admin audit trail, read back newest first."""

    async def list_admin_actions(
        self,
        *,
        actions: Sequence[str] = (),
        excluded_actions: Sequence[str] = (),
        before_id: int | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` audit rows, newest first.

        Newest first by ``id``, which only grows, so ``before_id`` pages
        through rows written while the page was open without repeating or
        skipping one.

        Args:
            actions: Keep only these. A name ending in ``.`` is a prefix
                (``agent.compute.`` keeps every compute action); any other is
                an exact action. Empty keeps every action.
            excluded_actions: Exact action names to leave out, even when
                ``actions`` would keep them.
            before_id: Keep only rows older than this one, for the next page.
            limit: The most rows to return.

        Returns:
            Rows with ``id``, ``action``, ``actor_id``, ``admin_ip``,
            ``target_user_id``, ``details`` (decoded, or ``None``),
            ``success`` and ``created_at``.
        """
        ...
