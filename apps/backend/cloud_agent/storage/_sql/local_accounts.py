"""What every local-account repository statement shares: the account read and its locks.

Lock order: an account row first (``FOR UPDATE`` for an edit, ``FOR SHARE`` for
opening a session), then its admission, identities, credential, sessions and
tokens. Every writer that can race another takes the account row, so a
suspension and a sign-in, or a password change and a sign-in, serialize on it
rather than leaving a session the revocation could not see. Two accounts are
locked in id order.

Whether an account's identity is verified, may sign in or may use Agent is
read from the view ``agent_local_account_access`` (migration
``0108_local_account_parity``), which every SQL gate shares. No statement
here derives it again from one identity kind.
"""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.domain.accounts import AccountNotFound, AccountRecord

ACCOUNT_COLUMNS = """
    a.id, e.value AS email, (e.verified_at IS NOT NULL) AS email_verified,
    n.value AS login_name, p.value AS phone, (c.account_id IS NOT NULL) AS has_password,
    x.identity_verified, a.status, a.is_admin, a.version, a.created_at,
    m.status AS admission_status, m.purpose, m.review_reason, m.submitted_at, m.reviewed_at,
    a.user_name, a.admin_note, a.suspension_message, a.last_login_at, a.deleted_at,
    a.erased_at
"""

ACCOUNT_FROM = """
    FROM agent_local_accounts a
    JOIN agent_local_admissions m ON m.account_id = a.id
    JOIN agent_local_account_access x ON x.account_id = a.id
    LEFT JOIN agent_local_login_identities e ON e.account_id = a.id AND e.kind = 'email'
    LEFT JOIN agent_local_login_identities n ON n.account_id = a.id AND n.kind = 'login_name'
    LEFT JOIN agent_local_login_identities p ON p.account_id = a.id AND p.kind = 'phone'
    LEFT JOIN agent_local_password_credentials c ON c.account_id = a.id
"""

REVOKE_SESSIONS_SQL = """
UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = $2
WHERE account_id = $1 AND revoked_at IS NULL
"""

#: Every session of the account except one (the caller's own).
REVOKE_OTHER_SESSIONS_SQL = """
UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = $2
WHERE account_id = $1 AND revoked_at IS NULL AND id IS DISTINCT FROM $3
"""

BUMP_VERSION_SQL = """
UPDATE agent_local_accounts SET version = version + 1, updated_at = NOW() WHERE id = $1
"""

#: An account without Agent use, by the shared access view.
ACCESS_ENDED_SQL = "NOT x.has_use"

AUDIT_SQL = """
INSERT INTO agent_admin_audit_log (action, actor_id, admin_ip, target_user_id, details, success)
VALUES ($1, $2, $3, $4, $5::jsonb, TRUE)
"""


def record(row: Any) -> AccountRecord:
    """The account a row of :data:`ACCOUNT_COLUMNS` describes."""
    return AccountRecord(
        id=row["id"],
        email=row["email"],
        email_verified=bool(row["email_verified"]),
        status=row["status"],
        is_admin=row["is_admin"],
        version=int(row["version"]),
        created_at=row["created_at"],
        admission_status=row["admission_status"],
        purpose=row["purpose"],
        review_reason=row["review_reason"],
        submitted_at=row["submitted_at"],
        reviewed_at=row["reviewed_at"],
        identity_verified=bool(row["identity_verified"]),
        login_name=row["login_name"],
        phone=row["phone"],
        has_password=bool(row["has_password"]),
        user_name=row["user_name"],
        admin_note=row["admin_note"],
        suspension_message=row["suspension_message"],
        last_login_at=row["last_login_at"],
        deleted_at=row["deleted_at"],
        erased_at=row["erased_at"],
    )


async def lock_account_row(conn: Any, account_id: str, *, mode: str = "UPDATE") -> bool:
    """Lock the account row alone; whether it exists.

    Alone, and before anything about the account is read: a statement that
    joined the other tables while it waited for the lock would see their rows
    as they were before the writer it waited for, under Read Committed.
    """
    return bool(
        await conn.fetchval(
            f"SELECT 1 FROM agent_local_accounts WHERE id = $1 FOR {mode}", account_id
        )
    )


async def read_account(conn: Any, account_id: str) -> AccountRecord:
    """The account as of this statement; :class:`AccountNotFound` if it is gone."""
    row = await conn.fetchrow(
        f"SELECT {ACCOUNT_COLUMNS} {ACCOUNT_FROM} WHERE a.id = $1", account_id
    )
    if row is None:
        raise AccountNotFound(account_id)
    return record(row)


async def locked_account(conn: Any, account_id: str) -> AccountRecord:
    """Lock the account row, then read it afresh under the lock."""
    if not await lock_account_row(conn, account_id):
        raise AccountNotFound(account_id)
    return await read_account(conn, account_id)


async def write_audit(
    conn: Any,
    *,
    action: str,
    actor_id: str,
    admin_ip: str,
    account_id: str | None,
    details: dict[str, Any],
) -> None:
    """One audit row in the caller's transaction. ``details`` never holds a credential."""
    await conn.execute(AUDIT_SQL, action, actor_id, admin_ip, account_id, json.dumps(details))


__all__ = [
    "ACCESS_ENDED_SQL",
    "ACCOUNT_COLUMNS",
    "ACCOUNT_FROM",
    "BUMP_VERSION_SQL",
    "REVOKE_OTHER_SESSIONS_SQL",
    "REVOKE_SESSIONS_SQL",
    "lock_account_row",
    "locked_account",
    "read_account",
    "record",
    "write_audit",
]
