"""Postgres implementation of the admin audit trail's read port.

Serves the admin Audit log page and nothing else. Rows are written by
:func:`cloud_agent.storage._sql.admin_audit.insert_audit_row`.

A ``requested`` row's final result comes from one of two places: the result a
worker reported (``agent_admin_audit_outcomes``, sandbox deletions), or the
compute operation the row names, whose own state is the result and is never
copied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from typing import Any

    import asyncpg

_SELECT = """
SELECT l.id, l.action, l.actor_id, l.actor_type, l.actor_label, l.admin_ip,
       l.target_user_id, l.subject_label, l.target_type, l.target_id, l.target_label,
       l.details, l.success, l.outcome, l.reason, l.correlation_id, l.created_at,
       COALESCE(r.outcome, CASE o.state
           WHEN 'succeeded' THEN 'applied'
           WHEN 'failed' THEN 'failed'
           WHEN 'cancelled' THEN 'cancelled'
       END) AS result,
       COALESCE(r.created_at, CASE WHEN o.state IN ('succeeded', 'failed', 'cancelled')
           THEN o.completed_at END) AS result_at,
       COALESCE(actor.status = 'deleted', FALSE) AS actor_deleted,
       COALESCE(subject.status = 'deleted', FALSE) AS subject_deleted
FROM agent_admin_audit_log l
LEFT JOIN agent_admin_audit_outcomes r ON r.event_id = l.id
LEFT JOIN agent_compute_operations o
       ON l.outcome = 'requested' AND starts_with(l.action, 'agent.compute.')
      AND o.id = l.correlation_id
LEFT JOIN agent_local_accounts actor ON actor.id = CASE
       WHEN starts_with(l.actor_id, 'admin:') THEN substr(l.actor_id, 7)
       WHEN starts_with(l.actor_id, 'self:') THEN substr(l.actor_id, 6)
     END
LEFT JOIN agent_local_accounts subject ON subject.id = l.target_user_id
"""


class PostgresAuditLogRepository:
    """The admin audit trail, read back newest first for the Audit log page."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

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
        """Return up to ``limit`` audit rows, newest first (see the port)."""
        conditions: list[str] = []
        args: list[Any] = []

        def bind(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if actions:
            conditions.append(f"l.action = ANY({bind(list(actions))}::text[])")
        if actor_types:
            conditions.append(f"l.actor_type = ANY({bind(list(actor_types))}::text[])")
        if subject_user_id is not None:
            # An account action's target is its affected user, so one column
            # answers both "acted on this account" and "on what it owns".
            conditions.append(f"l.target_user_id = {bind(subject_user_id)}")
        if target is not None:
            conditions.append(
                f"l.target_type = {bind(target[0])} AND l.target_id = {bind(target[1])}"
            )
        if before_id is not None:
            conditions.append(f"l.id < {bind(before_id)}")
        # Only the fixed conditions above and their placeholders are
        # interpolated; every value travels as a bound parameter.
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"{_SELECT} {where} ORDER BY l.id DESC LIMIT {bind(limit)}", *args
            )
        return [{**dict(row), "details": load_json(row["details"])} for row in rows]

    async def write_failures_since(self, since: datetime) -> tuple[int, datetime | None]:
        """How many audit writes failed since ``since``, and when the latest did."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS failures, max(created_at) AS latest "
                "FROM agent_admin_audit_write_failures WHERE created_at >= $1",
                since,
            )
        return int(row["failures"]), row["latest"]
