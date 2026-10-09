"""Postgres implementation of the admin audit trail's read port.

Serves the admin Audit log page and nothing else. The trail is written by
:class:`cloud_agent.storage.repositories.audit.PostgresAuditRepository`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    import asyncpg


def _prefix_pattern(prefix: str) -> str:
    """A ``LIKE`` pattern for names starting with ``prefix``, taken literally.

    Action names hold ``_`` (``agent.runner_host.drain``), which ``LIKE`` would
    otherwise read as "any one character".
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


class PostgresAuditLogRepository:
    """The admin audit trail, read back newest first for the Audit log page."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

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
        conditions: list[str] = []
        args: list[Any] = []

        def bind(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if actions:
            exact = [action for action in actions if not action.endswith(".")]
            prefixes = [_prefix_pattern(action) for action in actions if action.endswith(".")]
            conditions.append(
                f"(action = ANY({bind(exact)}::text[]) OR action LIKE ANY({bind(prefixes)}::text[]))"
            )
        if excluded_actions:
            conditions.append(f"NOT (action = ANY({bind(list(excluded_actions))}::text[]))")
        if before_id is not None:
            conditions.append(f"id < {bind(before_id)}")
        # Only the fixed conditions above and their placeholders are
        # interpolated; every value travels as a bound parameter.
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, action, actor_id, admin_ip, target_user_id, details,
                       success, created_at
                FROM agent_admin_audit_log
                {where}
                ORDER BY id DESC
                LIMIT {bind(limit)}
                """,
                *args,
            )
        return [{**dict(row), "details": load_json(row["details"])} for row in rows]
