"""The one place a row reaches the admin audit trail.

Every writer — the admin routes through
:class:`cloud_agent.storage.repositories.audit.PostgresAuditRepository`, and
the account, sandbox and team repositories inside their own transactions —
calls :func:`insert_audit_row`. It fills what a writer does not know from the
action's declaration (:mod:`cloud_agent.domain.audit`): the target, the
correlation and the outcome, and the names of the actor and the affected user
as they are now.

**Recording an action never makes the action fail.** The row is written in a
savepoint of the caller's transaction: the change and its record commit
together, and when the insert fails only the savepoint rolls back, the change
commits without it, and the failure is logged and counted in
``agent_admin_audit_write_failures`` for the Audit log to show.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.audit import actor_account_id, attribute, audit_action
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.domain.audit import FinalOutcome

logger = get_logger(__name__)

#: An account's name as the Audit log shows it: the login name, else the email,
#: the phone number or the profile name. Read when the row is written and kept,
#: so the row still names the account after it changes its name or is erased.
NAMES_SQL = """
SELECT a.id, COALESCE(n.value, e.value, p.value, a.user_name) AS label
FROM agent_local_accounts a
LEFT JOIN agent_local_login_identities n ON n.account_id = a.id AND n.kind = 'login_name'
LEFT JOIN agent_local_login_identities e ON e.account_id = a.id AND e.kind = 'email'
LEFT JOIN agent_local_login_identities p ON p.account_id = a.id AND p.kind = 'phone'
WHERE a.id = ANY($1::text[])
"""

INSERT_SQL = """
INSERT INTO agent_admin_audit_log
    (action, actor_id, admin_ip, target_user_id, details, success,
     actor_label, target_type, target_id, target_label, subject_label,
     outcome, reason, correlation_id)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, $13, $14)
RETURNING id
"""

_FAILURE_SQL = """
INSERT INTO agent_admin_audit_write_failures (action, actor_id, error) VALUES ($1, $2, $3)
"""

#: The request a ``requested`` row reported against, newest first: a sandbox
#: deletion resumed after its retries ran out keeps its request id.
_REQUEST_SQL = """
SELECT id FROM agent_admin_audit_log
WHERE correlation_id = $1 AND action = ANY($2::text[])
ORDER BY id DESC LIMIT 1
"""


#: The name the trail last recorded for an account, for one that no longer has
#: any: the erasure's own row is written after its identities are gone.
_REMEMBERED_SQL = """
SELECT ids.id, (
    SELECT l.subject_label FROM agent_admin_audit_log l
    WHERE l.target_user_id = ids.id AND l.subject_label IS NOT NULL
    ORDER BY l.id DESC LIMIT 1
) AS label
FROM unnest($1::text[]) AS ids(id)
"""


async def _names(conn: Any, *account_ids: str | None) -> dict[str, str]:
    wanted = sorted({account_id for account_id in account_ids if account_id})
    if not wanted:
        return {}
    rows = await conn.fetch(NAMES_SQL, wanted)
    names = {row["id"]: row["label"] for row in rows if row["label"]}
    missing = [account_id for account_id in wanted if account_id not in names]
    if missing:
        rows = await conn.fetch(_REMEMBERED_SQL, missing)
        names.update({row["id"]: row["label"] for row in rows if row["label"]})
    return names


async def _insert(
    conn: Any,
    *,
    action: str,
    actor_id: str,
    admin_ip: str,
    target_user_id: str | None,
    details: Mapping[str, Any] | None,
    success: bool,
    reason: str | None,
) -> int:
    attribution = attribute(
        action, details=details, subject_user_id=target_user_id, success=success
    )
    actor_account = actor_account_id(actor_id)
    names = await _names(conn, actor_account, target_user_id)
    subject_label = names.get(target_user_id) if target_user_id else None
    target_label = (
        subject_label
        if attribution.target_type == "account" and attribution.target_id == target_user_id
        else None
    )
    return await conn.fetchval(
        INSERT_SQL,
        action,
        actor_id,
        admin_ip,
        target_user_id,
        json.dumps(dict(details)) if details is not None else None,
        success,
        names.get(actor_account) if actor_account else None,
        attribution.target_type,
        attribution.target_id,
        target_label,
        subject_label,
        attribution.outcome,
        (reason or None) and reason[:2000],
        attribution.correlation_id,
    )


async def _count_failure(conn: Any, action: str, actor_id: str, error: BaseException) -> None:
    try:
        async with conn.transaction():
            await conn.execute(
                _FAILURE_SQL, action[:200], actor_id[:200], type(error).__name__[:200]
            )
    except Exception:
        logger.debug("agent_admin_audit_failure_uncounted", exc_info=True)


async def insert_audit_row(
    conn: Any,
    *,
    action: str,
    actor_id: str,
    admin_ip: str,
    target_user_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    success: bool = True,
    reason: str | None = None,
) -> int | None:
    """Append one row to the admin audit trail; never raise.

    Args:
        conn: The caller's connection, inside its transaction when the change
            is a database write, so the change and its record commit together.
        action: The dotted action name, declared in
            :data:`cloud_agent.domain.audit.ACTIONS`.
        actor_id: Who acted, in the trail's actor shape (``admin:<id>`` ...).
        admin_ip: The caller's address, best effort.
        target_user_id: The user affected, where there is one.
        details: Structured context. Never a credential: this trail is read
            by more people than hold the things it would otherwise contain.
        success: Whether the action did what it was asked.
        reason: Why, in the administrator's words, when they gave one.

    Returns:
        The new row's id, or ``None`` when it could not be written.
    """
    if audit_action(action) is None:
        logger.error(
            "agent_admin_audit_undeclared_action",
            extra={"event": "agent_admin_audit_undeclared_action", "action": action},
        )
    try:
        async with conn.transaction():
            return await _insert(
                conn,
                action=action,
                actor_id=actor_id,
                admin_ip=admin_ip,
                target_user_id=target_user_id,
                details=details,
                success=success,
                reason=reason,
            )
    except Exception as exc:
        logger.error(
            "agent_admin_audit_write_failed",
            exc_info=True,
            extra={
                "event": "agent_admin_audit_write_failed",
                "action": action,
                "actor_id": actor_id,
                "target_user_id": target_user_id,
            },
        )
        await _count_failure(conn, action, actor_id, exc)
        return None


async def report_final_outcome(
    conn: Any,
    *,
    requested_actions: tuple[str, ...],
    correlation_id: str,
    outcome: FinalOutcome,
    reported_by: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Set the final result of the newest matching ``requested`` row; never raise.

    A request recorded by no row is left alone.
    """
    try:
        async with conn.transaction():
            event_id = await conn.fetchval(_REQUEST_SQL, correlation_id, list(requested_actions))
            if event_id is None:
                return
            await conn.execute(
                """
                INSERT INTO agent_admin_audit_outcomes (event_id, outcome, reported_by, details)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (event_id) DO UPDATE
                SET outcome = EXCLUDED.outcome, reported_by = EXCLUDED.reported_by,
                    details = EXCLUDED.details, created_at = NOW()
                """,
                event_id,
                outcome,
                reported_by,
                json.dumps(dict(details or {})),
            )
    except Exception:
        logger.error(
            "agent_admin_audit_outcome_write_failed",
            exc_info=True,
            extra={
                "event": "agent_admin_audit_outcome_write_failed",
                "correlation_id": correlation_id,
            },
        )


__all__ = ["NAMES_SQL", "insert_audit_row", "report_final_outcome"]
