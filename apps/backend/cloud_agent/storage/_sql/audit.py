"""The admin audit row an administrator's action writes."""

from __future__ import annotations

import json
from typing import Any


async def insert_admin_audit(
    conn: Any,
    *,
    action: str,
    actor_id: str,
    admin_ip: str,
    target_user_id: str | None,
    details: dict[str, Any] | None,
    success: bool = True,
) -> int:
    """Append one row to the admin audit trail, in the caller's transaction.

    A mutation and its audit row commit together, so an unavailable audit
    table never leaves an unrecorded cross-owner change. ``details`` is
    structured context and never a credential. Returns the new row's id.
    """
    return await conn.fetchval(
        "INSERT INTO agent_admin_audit_log "
        "(action, actor_id, admin_ip, target_user_id, details, success) "
        "VALUES ($1, $6, $2, $3, $4::jsonb, $5) RETURNING id",
        action,
        admin_ip,
        target_user_id,
        json.dumps(details) if details is not None else None,
        success,
        actor_id,
    )
