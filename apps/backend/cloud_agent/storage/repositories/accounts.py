"""Postgres implementation of the local-account port: accounts, admission and termination.

Sessions and browsers, emailed tokens and credentials, and the policy tables
(phone codes, sign-in events, signup domains, first-run setup) are mixins in
their own modules; this class assembles them over one pool. The lock order
and the shared account read are in :mod:`cloud_agent.storage._sql.local_accounts`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.accounts import (
    ACCESS_GRANTING_ACTIONS,
    AccountNotFound,
    AccountRecord,
    AccountTransitionRefused,
    AccountVersionConflict,
    EmailTaken,
    permissions_for,
    plan_admin_action,
    plan_self_deletion,
    plan_waitlist_submission,
    require_actor_admin,
)
from cloud_agent.ports.accounts import (
    AdminActionResult,
    EmailVerifiedBy,
    ErasureResult,
    PasswordLogin,
)
from cloud_agent.storage._sql.advisory import LOCK_KEY_SQL, finish_lock_cleanup
from cloud_agent.storage._sql.local_accounts import (
    ACCESS_ENDED_SQL,
    ACCOUNT_COLUMNS,
    ACCOUNT_FROM,
    BUMP_VERSION_SQL,
    REVOKE_SESSIONS_SQL,
    locked_account,
    read_account,
    record,
    write_audit,
)
from cloud_agent.storage._sql.local_erasure import (
    ErasureBlocked,
    erase_owner_rows,
    require_hosts_clean,
    retained_archives,
    settle_teams,
)
from cloud_agent.storage.repositories.account_broadcasts import BroadcastsMixin
from cloud_agent.storage.repositories.account_credentials import CredentialsMixin
from cloud_agent.storage.repositories.account_policy import PolicyMixin
from cloud_agent.storage.repositories.account_sessions import SessionsMixin
from cloud_agent.storage.repositories.account_teams import TeamsMixin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from cloud_agent.domain.accounts import AdmissionEdit
    from cloud_agent.ports.accounts import IdentifierKind

#: Termination passes one process runs at once. Each holds one connection for
#: its lock and borrows one more at a time, so two leave most of the pool free.
TERMINATION_CONCURRENCY = 2

#: Searched by the administration page: identifiers, display name, id prefix.
_SEARCH_SQL = """
($3::text IS NULL OR a.id LIKE upper($3) || '%' ESCAPE '\\'
 OR e.value_normalized LIKE '%' || lower($3) || '%' ESCAPE '\\'
 OR n.value_normalized LIKE '%' || lower($3) || '%' ESCAPE '\\'
 OR p.value_normalized LIKE '%' || $3 || '%' ESCAPE '\\'
 OR lower(a.user_name) LIKE '%' || lower($3) || '%' ESCAPE '\\')
"""


def _like_term(query: str | None) -> str | None:
    term = (query or "").strip()
    if not term:
        return None
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _lock_target_and_actor(
    conn: Any, account_id: str, actor_account_id: str | None
) -> AccountRecord:
    """Lock the target and the acting administrator in id order; the target's record.

    Id order is the one every two-account writer takes, so two administrators
    acting on each other cannot deadlock. The actor's permission is judged
    under its lock, in this transaction.
    """
    if actor_account_id is None or actor_account_id == account_id:
        target = await locked_account(conn, account_id)
        if actor_account_id is not None:
            require_actor_admin(target)
        return target
    locked: dict[str, AccountRecord | None] = {}
    for locked_id in sorted((account_id, actor_account_id)):
        try:
            locked[locked_id] = await locked_account(conn, locked_id)
        except AccountNotFound:
            if locked_id == account_id:
                raise
            locked[locked_id] = None
    require_actor_admin(locked[actor_account_id])
    found = locked[account_id]
    assert found is not None
    return found


def termination_lock_key(account_id: str) -> str:
    """The advisory lock a termination pass holds while it stops an account's work.

    Approving the account or making it an administrator takes the same lock
    in its own transaction, so neither commits while a pass, from any process,
    is still stopping work, and a pass that starts afterwards sees the grant.
    """
    return f"cloud-agent:local-access-termination:{account_id}"


async def _cleanup_open(conn: Any, account_id: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM agent_local_access_terminations "
            "WHERE account_id = $1 AND completed_at IS NULL)",
            account_id,
        )
    )


async def _refuse_grant_while_cleanup_open(conn: Any, account_id: str) -> None:
    if await _cleanup_open(conn, account_id) or not await conn.fetchval(
        f"SELECT pg_try_advisory_xact_lock({LOCK_KEY_SQL})", termination_lock_key(account_id)
    ):
        raise AccountTransitionRefused(
            "The account's earlier access is still being stopped; try again shortly.",
            code="access_cleanup_pending",
        )


async def _require_erasable(conn: Any, current: AccountRecord) -> None:
    busy = await conn.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM agent_jobs WHERE user_id = $1
                       AND state IN ('queued', 'running'))
            OR EXISTS (SELECT 1 FROM agent_threads t
                       JOIN agent_thread_sandbox_memberships sm ON sm.thread_id = t.id
                       WHERE t.user_id = $1 AND sm.residency_status <> 'none')
        """,
        current.id,
    )
    if current.status != "deleted" or busy or await _cleanup_open(conn, current.id):
        raise AccountTransitionRefused(
            "Only a deleted account whose work has fully stopped can be erased.",
            code="erase_blocked",
        )


def _audit_details(record_: AccountRecord, edit: AdmissionEdit, version: int) -> dict[str, Any]:
    # States and the version only: no email, password, hash or token.
    return {
        "from": {
            "status": record_.status,
            "admission": record_.admission_status,
            "is_admin": record_.is_admin,
        },
        "to": {
            "status": edit.account_status,
            "admission": edit.admission_status,
            "is_admin": edit.is_admin,
        },
        "version": version,
        "reason_given": edit.review_reason is not None,
    }


async def _insert_identity(
    conn: Any, account_id: str, kind: str, value: str, normalized: str, *, verified_by: str | None
) -> None:
    """Add one identity, verified by ``verified_by`` or not yet.

    :class:`EmailTaken` rolls the caller's transaction back if it is taken.
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO agent_local_login_identities
            (account_id, kind, value, value_normalized, verified_at, verified_by)
        VALUES ($1, $2, $3, $4, CASE WHEN $5::text IS NOT NULL THEN NOW() END, $5)
        ON CONFLICT (kind, value_normalized) DO NOTHING
        RETURNING 1
        """,
        account_id,
        kind,
        value,
        normalized,
        verified_by,
    )
    if inserted is None:
        raise EmailTaken


class PostgresLocalAccountRepository(
    SessionsMixin, CredentialsMixin, PolicyMixin, BroadcastsMixin, TeamsMixin
):
    """Local accounts over the control plane's pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool
        self._termination_slots = asyncio.Semaphore(TERMINATION_CONCURRENCY)

    # ── Creating accounts ────────────────────────────────────────────────

    async def create_account(
        self,
        *,
        account_id: str,
        email: str,
        email_normalized: str,
        password_hash: str,
        user_name: str | None = None,
        email_verified_by: EmailVerifiedBy | None = None,
        tos_accepted: bool = False,
        purpose: str | None = None,
    ) -> bool:
        """Create an email-and-password account; ``False`` if the email is taken.

        The email starts unverified, or verified by ``policy`` (verification
        is off: accepted, not proven) or ``operator`` (made on the command
        line). ``purpose`` submits the Agent application at once, as a
        signup that waits for approval does; otherwise the account has not
        applied yet.
        """
        return await self._create(
            account_id,
            [("email", email, email_normalized, email_verified_by)],
            password_hash=password_hash,
            user_name=user_name,
            tos_accepted=tos_accepted,
            purpose=purpose,
        )

    async def create_login_name_account(
        self,
        *,
        account_id: str,
        login_name: str,
        password_hash: str,
        user_name: str | None = None,
    ) -> bool:
        """Create an account that signs in by login name; ``False`` if the name is taken."""
        return await self._create(
            account_id,
            [("login_name", login_name, login_name, "operator")],
            password_hash=password_hash,
            user_name=user_name,
            tos_accepted=False,
            purpose=None,
        )

    async def create_phone_account(
        self,
        *,
        account_id: str,
        phone: str,
        user_name: str | None,
        tos_accepted: bool,
        purpose: str | None,
    ) -> bool:
        """Create an account proven by a phone code, without a password; ``False`` if taken."""
        return await self._create(
            account_id,
            [("phone", phone, phone, "code")],
            password_hash=None,
            user_name=user_name,
            tos_accepted=tos_accepted,
            purpose=purpose,
        )

    async def _create(
        self,
        account_id: str,
        identities: list[tuple[str, str, str, str | None]],
        *,
        password_hash: str | None,
        user_name: str | None,
        tos_accepted: bool,
        purpose: str | None,
    ) -> bool:
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                for kind, _value, normalized, _verified in identities:
                    if await conn.fetchval(
                        "SELECT 1 FROM agent_local_login_identities "
                        "WHERE kind = $1 AND value_normalized = $2",
                        kind,
                        normalized,
                    ):
                        return False
                await conn.execute(
                    "INSERT INTO agent_local_accounts (id, user_name, tos_accepted_at) "
                    "VALUES ($1, $2, CASE WHEN $3 THEN NOW() END)",
                    account_id,
                    user_name,
                    tos_accepted,
                )
                for kind, value, normalized, verified_by in identities:
                    # A concurrent sign-up that took the identifier after the
                    # check above rolls this account back with the transaction.
                    await _insert_identity(
                        conn, account_id, kind, value, normalized, verified_by=verified_by
                    )
                if password_hash is not None:
                    await conn.execute(
                        "INSERT INTO agent_local_password_credentials (account_id, password_hash) "
                        "VALUES ($1, $2)",
                        account_id,
                        password_hash,
                    )
                if purpose is None:
                    await conn.execute(
                        "INSERT INTO agent_local_admissions (account_id) VALUES ($1)", account_id
                    )
                else:
                    await conn.execute(
                        "INSERT INTO agent_local_admissions (account_id, status, purpose, "
                        "submitted_at) VALUES ($1, 'pending', $2, NOW())",
                        account_id,
                        purpose,
                    )
                return True
        except EmailTaken:
            return False

    # ── Reading accounts ─────────────────────────────────────────────────

    async def password_login(self, email_normalized: str) -> PasswordLogin | None:
        """Return the account signed in with this email, or ``None``."""
        return await self.find_login("email", email_normalized)

    async def find_login(self, kind: IdentifierKind, normalized: str) -> PasswordLogin | None:
        """The account an identifier signs in to, with its password hash if it has one."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {ACCOUNT_COLUMNS}, c.password_hash {ACCOUNT_FROM} "
                "WHERE a.id = (SELECT account_id FROM agent_local_login_identities "
                "WHERE kind = $1 AND value_normalized = $2)",
                kind,
                normalized,
            )
        if row is None:
            return None
        return PasswordLogin(account=record(row), password_hash=row["password_hash"])

    async def get_account(self, account_id: str) -> AccountRecord | None:
        """Return one account, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {ACCOUNT_COLUMNS} {ACCOUNT_FROM} WHERE a.id = $1", account_id
            )
        return record(row) if row is not None else None

    async def account_emails(self, account_ids: list[str]) -> dict[str, str]:
        """Return the email of each listed account that has one."""
        if not account_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT account_id, value FROM agent_local_login_identities "
                "WHERE kind = 'email' AND account_id = ANY($1::text[])",
                list(account_ids),
            )
        return {row["account_id"]: row["value"] for row in rows}

    async def list_accounts(
        self,
        *,
        admission_status: str | None,
        limit: int,
        offset: int,
        account_status: str | None = None,
        query: str | None = None,
        sort: str = "created",
    ) -> tuple[list[AccountRecord], int]:
        """Return one page of accounts and the filtered total.

        ``sort`` is ``created`` (newest first, the default) or ``last_login``
        (most recently signed in first, never-signed-in last), as
        HybridInference's account list sorts.
        """
        term = _like_term(query)
        order = (
            "a.last_login_at DESC NULLS LAST, a.created_at DESC, a.id DESC"
            if sort == "last_login"
            else "a.created_at DESC, a.id DESC"
        )
        where = (
            "WHERE ($1::text IS NULL OR m.status = $1) AND ($2::text IS NULL OR a.status = $2) "
            f"AND {_SEARCH_SQL}"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {ACCOUNT_COLUMNS} {ACCOUNT_FROM} {where} "
                f"ORDER BY {order} LIMIT $4 OFFSET $5",
                admission_status,
                account_status,
                term,
                limit,
                offset,
            )
            total = await conn.fetchval(
                f"SELECT count(*) {ACCOUNT_FROM} {where}", admission_status, account_status, term
            )
        return [record(row) for row in rows], int(total)

    async def account_counts(self) -> dict[str, int]:
        """How many accounts are in each account state and each application state."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT 'status:' || a.status AS k, count(*) AS n FROM agent_local_accounts a "
                "GROUP BY a.status UNION ALL "
                "SELECT 'admission:' || m.status, count(*) FROM agent_local_admissions m "
                "GROUP BY m.status"
            )
            total = await conn.fetchval("SELECT count(*) FROM agent_local_accounts")
        counts = {str(row["k"]): int(row["n"]) for row in rows}
        counts["all"] = int(total)
        return counts

    # ── The waiting list and administration ──────────────────────────────

    async def submit_application(self, *, account_id: str, purpose: str) -> AccountRecord:
        """Apply, or resubmit after a rejection or revocation; repeating pending is a no-op."""
        async with self._pool.acquire() as conn, conn.transaction():
            current = await locked_account(conn, account_id)
            if not plan_waitlist_submission(current):
                return current
            await conn.execute(
                """
                UPDATE agent_local_admissions
                SET status = 'pending', purpose = $2, review_reason = NULL,
                    submitted_at = NOW(), reviewed_at = NULL, reviewed_by = NULL
                WHERE account_id = $1
                """,
                account_id,
                purpose,
            )
            await conn.execute(BUMP_VERSION_SQL, account_id)
            return await read_account(conn, account_id)

    async def apply_admin_action(
        self,
        *,
        account_id: str,
        action: str,
        reason: str | None,
        expected_version: int,
        actor_id: str,
        actor_account_id: str | None,
        admin_ip: str,
    ) -> AdminActionResult:
        """Apply one administrator action and audit it, at the version the admin read."""
        async with self._pool.acquire() as conn, conn.transaction():
            current = await _lock_target_and_actor(conn, account_id, actor_account_id)
            if current.version != expected_version:
                raise AccountVersionConflict(current.version)
            if action in ACCESS_GRANTING_ACTIONS:
                await _refuse_grant_while_cleanup_open(conn, account_id)
            if action == "resume" and await _cleanup_open(conn, account_id):
                raise AccountTransitionRefused(
                    "The account's work is still being stopped; try again shortly.",
                    code="access_cleanup_pending",
                )
            edit = plan_admin_action(
                current, action, reason=reason, actor_account_id=actor_account_id
            )
            await self._write_edit(conn, current, edit, actor_id=actor_id)
            await self._open_termination(conn, current, edit, action)
            await write_audit(
                conn,
                action=f"agent.local_account.{action}",
                actor_id=actor_id,
                admin_ip=admin_ip,
                account_id=account_id,
                details=_audit_details(current, edit, current.version + 1),
            )
            return AdminActionResult(account=await read_account(conn, account_id), edit=edit)

    async def approve_by_policy(self, *, account_id: str, actor_id: str) -> AccountRecord | None:
        """Approve a verified, active account the signup policy admits; ``None`` if it may not.

        The automatic counterpart of an administrator's approval, in its own
        audited transaction: refused (``None``) unless the account is active,
        verified, not yet approved and has no open termination.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                current = await locked_account(conn, account_id)
            except AccountNotFound:
                return None
            if (
                current.status != "active"
                or not current.identity_verified
                or current.admission_status in ("approved", "rejected", "revoked")
            ):
                return None
            try:
                await _refuse_grant_while_cleanup_open(conn, account_id)
                edit = plan_admin_action(current, "approve", reason=None, actor_account_id=None)
            except AccountTransitionRefused:
                return None
            await self._write_edit(conn, current, edit, actor_id=actor_id)
            await write_audit(
                conn,
                action="agent.local_account.approve",
                actor_id=actor_id,
                admin_ip="local",
                account_id=account_id,
                details=_audit_details(current, edit, current.version + 1),
            )
            return await read_account(conn, account_id)

    async def annotate_account(
        self,
        *,
        account_id: str,
        admin_note: str | None,
        suspension_message: str | None,
        fields: frozenset[str],
        expected_version: int,
        actor_id: str,
        actor_account_id: str | None,
        admin_ip: str,
    ) -> AccountRecord:
        """Set the administrator's note and/or the message a suspended account is shown."""
        async with self._pool.acquire() as conn, conn.transaction():
            current = await _lock_target_and_actor(conn, account_id, actor_account_id)
            if current.version != expected_version:
                raise AccountVersionConflict(current.version)
            await conn.execute(
                "UPDATE agent_local_accounts SET "
                "admin_note = CASE WHEN $2 THEN $3 ELSE admin_note END, "
                "suspension_message = CASE WHEN $4 THEN $5 ELSE suspension_message END, "
                "version = version + 1, updated_at = NOW() WHERE id = $1",
                account_id,
                "admin_note" in fields,
                admin_note,
                "suspension_message" in fields,
                suspension_message,
            )
            await write_audit(
                conn,
                action="agent.local_account.annotate",
                actor_id=actor_id,
                admin_ip=admin_ip,
                account_id=account_id,
                details={"fields": sorted(fields), "version": current.version + 1},
            )
            return await read_account(conn, account_id)

    async def delete_own_account(
        self, *, account_id: str, expected_version: int, admin_ip: str
    ) -> AccountRecord:
        """Delete the caller's account at the version its owner confirmed.

        Raises:
            AccountVersionConflict: It changed after the owner confirmed.
            SelfDeleteRefused: An administrator, or not active.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await locked_account(conn, account_id)
            if current.version != expected_version:
                raise AccountVersionConflict(current.version)
            edit = plan_self_deletion(current)
            await self._write_edit(conn, current, edit, actor_id=f"self:{account_id}")
            await self._open_termination(conn, current, edit, "delete")
            await write_audit(
                conn,
                action="agent.local_account.self_delete",
                actor_id=f"self:{account_id}",
                admin_ip=admin_ip,
                account_id=account_id,
                details=_audit_details(current, edit, current.version + 1),
            )
            return await read_account(conn, account_id)

    async def erase_account(
        self,
        *,
        account_id: str,
        actor_id: str,
        actor_account_id: str | None,
        admin_ip: str,
    ) -> ErasureResult:
        """Erase a deleted account and all its Agent data; its id stays as a tombstone.

        Refused (``erase_blocked``) until the account is deleted, its
        termination is acknowledged, none of its turns is queued, running or
        resident and every sandbox it owns is deleted, or when erasing would
        touch another owner's row; (``host_cleanup_pending``) while a host
        still has to clean up one of its environment runs; and
        (``team_transfer_required``) while it owns a team other members use.
        Then, in one transaction, its teams are settled and every row of its
        Agent data goes (:mod:`cloud_agent.storage._sql.local_erasure`), with
        its identifiers (freed for new accounts), password, sessions,
        browsers, links, codes, sign-in events, mail recipients and earlier
        audit rows. The account row stays, erased: it signs in nowhere and
        cannot be resumed, and its id never names anything again. A fresh
        audit row records the erasure.

        Raises:
            AccountTransitionRefused: The codes above.
        """
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                current = await _lock_target_and_actor(conn, account_id, actor_account_id)
                await _require_erasable(conn, current)
                await require_hosts_clean(conn, account_id)
                teams = await settle_teams(conn, account_id)
                retained = await retained_archives(conn, account_id)
                deleted = await erase_owner_rows(conn, account_id)
                await self._erase_identity(conn, account_id)
                await write_audit(
                    conn,
                    action="agent.local_account.erase",
                    actor_id=actor_id,
                    admin_ip=admin_ip,
                    account_id=account_id,
                    details={
                        "version": current.version + 1,
                        "rows": deleted,
                        "retained_archives": retained,
                        **teams,
                    },
                )
                return ErasureResult(
                    account=await read_account(conn, account_id),
                    deleted=deleted,
                    retained_archives=retained,
                    deleted_teams=teams["deleted_teams"],
                    handed_environments=teams["handed_environments"],
                )
        except ErasureBlocked as exc:
            raise AccountTransitionRefused(str(exc), code=exc.code) from None
        except asyncpg.ForeignKeyViolationError as exc:
            where = getattr(exc, "table_name", None)
            raise AccountTransitionRefused(
                "The account's data is still referenced"
                + (f" by {where}" if where else " elsewhere")
                + " and cannot be erased.",
                code="erase_blocked",
            ) from None

    async def _erase_identity(self, conn: Any, account_id: str) -> None:
        for table in (
            "agent_local_login_identities",
            "agent_local_password_credentials",
            "agent_local_sessions",
            "agent_local_auth_tokens",
            "agent_local_login_events",
            "agent_local_broadcast_recipients",
        ):
            await conn.execute(f"DELETE FROM {table} WHERE account_id = $1", account_id)
        await conn.execute("DELETE FROM agent_local_phone_codes WHERE account_key = $1", account_id)
        await conn.execute(
            "UPDATE agent_local_accounts SET erased_at = coalesce(erased_at, NOW()), "
            "user_name = NULL, admin_note = NULL, suspension_message = NULL, "
            "tos_accepted_at = NULL, locale = NULL, locale_chosen_at = NULL, "
            "last_login_at = NULL, "
            "version = version + 1, updated_at = NOW() WHERE id = $1",
            account_id,
        )
        await conn.execute(
            "UPDATE agent_local_admissions SET purpose = '', review_reason = NULL "
            "WHERE account_id = $1",
            account_id,
        )

    async def _open_termination(
        self, conn: Any, current: AccountRecord, edit: AdmissionEdit, action: str
    ) -> None:
        if edit.ends_access:
            await conn.execute(
                "INSERT INTO agent_local_access_terminations (account_id, generation, action) "
                "VALUES ($1, $2, $3)",
                current.id,
                current.version + 1,
                "delete" if edit.account_status == "deleted" else action,
            )

    async def _write_edit(
        self, conn: Any, current: AccountRecord, edit: AdmissionEdit, *, actor_id: str
    ) -> None:
        await conn.execute(
            "UPDATE agent_local_accounts SET status = $2, is_admin = $3, "
            "deleted_at = CASE WHEN $2 = 'deleted' THEN coalesce(deleted_at, NOW()) END, "
            "version = version + 1, updated_at = NOW() WHERE id = $1",
            current.id,
            edit.account_status,
            edit.is_admin,
        )
        await conn.execute(
            """
            UPDATE agent_local_admissions
            SET status = $2,
                review_reason = CASE WHEN $3 THEN $4 ELSE review_reason END,
                reviewed_at = CASE WHEN $3 THEN NOW() ELSE reviewed_at END,
                reviewed_by = CASE WHEN $3 THEN $5 ELSE reviewed_by END
            WHERE account_id = $1
            """,
            current.id,
            edit.admission_status,
            edit.reviewed,
            edit.review_reason,
            actor_id,
        )
        if edit.account_status == "deleted":
            # Its browsers before its sessions: the order a sign-in takes.
            await conn.execute(
                "SELECT 1 FROM agent_local_browser_sessions WHERE id IN (SELECT browser_id FROM "
                "agent_local_browser_accounts WHERE account_id = $1) ORDER BY id FOR UPDATE",
                current.id,
            )
        if edit.account_status != "active" and current.status == "active":
            await conn.execute(REVOKE_SESSIONS_SQL, current.id, edit.account_status)
        if edit.account_status == "deleted":
            # Off every browser and with no way back in: as HybridInference's
            # soft delete purges sessions and tokens.
            await conn.execute(REVOKE_SESSIONS_SQL, current.id, "deleted")
            await conn.execute(
                "DELETE FROM agent_local_browser_accounts WHERE account_id = $1", current.id
            )
            await conn.execute(
                "UPDATE agent_local_browser_sessions SET active_account_id = NULL "
                "WHERE active_account_id = $1",
                current.id,
            )
            await conn.execute(
                "UPDATE agent_local_auth_tokens SET consumed_at = NOW() "
                "WHERE account_id = $1 AND consumed_at IS NULL",
                current.id,
            )
            # Its open phone codes go too, as HybridInference forgets them.
            await conn.execute(
                "DELETE FROM agent_local_phone_codes WHERE account_key = $1", current.id
            )

    async def promote_listed_admin(self, *, account_id: str, actor_id: str) -> AccountRecord | None:
        """Make an account whose verified email is a configured admin address an administrator.

        HybridInference's ``ADMIN_EMAILS`` seed: the account is approved (when
        it is not yet) and granted administration, each audited as
        ``actor_id``, in one transaction. ``None`` when nothing changed: not
        active, no proven email (one accepted because verification is off
        does not count), already an administrator, or an earlier
        revocation's cleanup is still open.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                current = await locked_account(conn, account_id)
            except AccountNotFound:
                return None
            if (
                current.is_admin
                or current.status != "active"
                or not (current.email and current.email_proven and current.identity_verified)
            ):
                return None
            try:
                await _refuse_grant_while_cleanup_open(conn, account_id)
            except AccountTransitionRefused:
                return None
            for action in (
                ("grant_admin",)
                if current.admission_status == "approved"
                else ("approve", "grant_admin")
            ):
                edit = plan_admin_action(current, action, reason=None, actor_account_id=None)
                await self._write_edit(conn, current, edit, actor_id=actor_id)
                await write_audit(
                    conn,
                    action=f"agent.local_account.{action}",
                    actor_id=actor_id,
                    admin_ip="local",
                    account_id=account_id,
                    details=_audit_details(current, edit, current.version + 1),
                )
                current = await read_account(conn, account_id)
            return current

    async def set_locale(
        self, *, account_id: str, locale: str, chosen_at: datetime
    ) -> tuple[str | None, datetime | None]:
        """Store the interface language if it is the most recent choice; the one kept.

        A choice made earlier than the stored one (on another browser
        meanwhile) is not written, in the same statement as the comparison,
        so overlapping requests cannot leave the older choice.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_accounts SET locale = $2, locale_chosen_at = $3 "
                "WHERE id = $1 AND (locale_chosen_at IS NULL OR locale_chosen_at < $3)",
                account_id,
                locale,
                chosen_at,
            )
            row = await conn.fetchrow(
                "SELECT locale, locale_chosen_at FROM agent_local_accounts WHERE id = $1",
                account_id,
            )
        if row is None:
            raise AccountNotFound(account_id)
        return row["locale"], row["locale_chosen_at"]

    # ── Abuse counters ───────────────────────────────────────────────────

    async def hit_rate_limit(
        self, *, bucket: str, key_hash: bytes, window_s: int, limit: int
    ) -> float:
        """Count one attempt; seconds until the window reopens if over ``limit``, else 0."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH w AS (
                    SELECT to_timestamp(floor(extract(epoch FROM NOW()) / $3) * $3) AS start
                )
                INSERT INTO agent_local_auth_rate_limits
                    (bucket, key_hash, window_start, window_end, hits)
                SELECT $1, $2, w.start, w.start + make_interval(secs => $3), 1 FROM w
                ON CONFLICT (bucket, key_hash, window_start)
                DO UPDATE SET hits = agent_local_auth_rate_limits.hits + 1
                RETURNING hits, extract(epoch FROM window_end - NOW()) AS remaining
                """,
                bucket,
                key_hash,
                int(window_s),
            )
            if int(row["hits"]) == 1:
                await conn.execute(
                    "DELETE FROM agent_local_auth_rate_limits WHERE ctid IN ("
                    "SELECT ctid FROM agent_local_auth_rate_limits "
                    "WHERE window_end < NOW() LIMIT 200)"
                )
        if int(row["hits"]) <= limit:
            return 0.0
        return max(1.0, float(row["remaining"]))

    # ── Stopping an account's work ───────────────────────────────────────

    @contextlib.asynccontextmanager
    async def termination_lock(self, account_id: str) -> AsyncIterator[bool]:
        """Hold the account's termination lock for a whole pass; ``False`` if another holds it.

        Session-level, on a connection kept for the pass, so it spans the host
        calls between the pass's transactions. At most
        :data:`TERMINATION_CONCURRENCY` passes hold one at a time, so the
        queries a pass makes on other connections always find one free. The
        lock is released however the pass ends, cancellation included, and a
        connection whose release is uncertain is reset or closed rather than
        returned to the pool holding it.
        """
        key = termination_lock_key(account_id)
        async with self._termination_slots, self._pool.acquire() as conn:
            known_free = False
            try:
                held = bool(
                    await conn.fetchval(f"SELECT pg_try_advisory_lock({LOCK_KEY_SQL})", key)
                )
                known_free = not held
                yield held
            finally:
                if not known_free:
                    await finish_lock_cleanup(conn, key)

    async def reopen_termination(self, account_id: str) -> bool:
        """Open (or reopen) a termination at the account's version, if it has no access.

        For work that outlived a pass: a terminal opened by a request admitted
        before the revocation. The open record is what the enforcement pass
        retries, and what keeps re-approval waiting until it is cleaned up.
        Each reopen bumps the record's revision, so a pass already under way,
        which read the earlier revision, cannot close it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                current = await locked_account(conn, account_id)
            except AccountNotFound:
                return False
            if permissions_for(current):
                return False
            action = {"suspended": "suspend", "deleted": "delete"}.get(current.status, "revoke")
            await conn.execute(
                """
                INSERT INTO agent_local_access_terminations (account_id, generation, action)
                VALUES ($1, $2, $3)
                ON CONFLICT (account_id, generation)
                DO UPDATE SET completed_at = NULL, last_outcome = 'reopened',
                              revision = agent_local_access_terminations.revision + 1
                """,
                account_id,
                current.version,
                action,
            )
            return True

    async def open_terminations(self, *, limit: int) -> list[tuple[str, int]]:
        """Revocations whose cleanup is not yet acknowledged, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT account_id, generation FROM agent_local_access_terminations "
                "WHERE completed_at IS NULL ORDER BY created_at, account_id LIMIT $1",
                limit,
            )
        return [(row["account_id"], int(row["generation"])) for row in rows]

    async def termination_open(self, account_id: str) -> bool:
        """Whether a termination of this account is still waiting for its cleanup."""
        async with self._pool.acquire() as conn:
            return await _cleanup_open(conn, account_id)

    async def termination_revision(self, *, account_id: str, generation: int) -> int | None:
        """The record's current revision, read by a pass before it starts its cleanup."""
        async with self._pool.acquire() as conn:
            revision = await conn.fetchval(
                "SELECT revision FROM agent_local_access_terminations "
                "WHERE account_id = $1 AND generation = $2",
                account_id,
                generation,
            )
        return None if revision is None else int(revision)

    async def record_termination_attempt(
        self, *, account_id: str, generation: int, revision: int, complete: bool, outcome: str
    ) -> bool:
        """Count one cleanup attempt; close the record only at the revision the pass read.

        The comparison is made under the row's lock in the UPDATE itself, so a
        reopen that commits first is seen here and a later one reopens a
        closed record. Returns whether the record is now closed.
        """
        async with self._pool.acquire() as conn:
            closed = await conn.fetchval(
                """
                UPDATE agent_local_access_terminations
                SET attempts = attempts + 1, last_attempt_at = NOW(),
                    last_outcome = CASE WHEN $4 AND revision <> $5 THEN 'reopened' ELSE $3 END,
                    completed_at = CASE WHEN $4 AND revision = $5 THEN NOW() ELSE NULL END
                WHERE account_id = $1 AND generation = $2 AND completed_at IS NULL
                RETURNING completed_at IS NOT NULL
                """,
                account_id,
                generation,
                outcome,
                complete,
                revision,
            )
            if closed is None:
                # Already closed, by an earlier pass at this revision.
                closed = await conn.fetchval(
                    "SELECT completed_at IS NOT NULL FROM agent_local_access_terminations "
                    "WHERE account_id = $1 AND generation = $2",
                    account_id,
                    generation,
                )
        return bool(closed)

    async def accounts_with_ended_access(self, *, limit: int) -> list[str]:
        """Accounts without Agent use that still have queued, running or resident work."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT a.id {ACCOUNT_FROM}
                WHERE {ACCESS_ENDED_SQL}
                  AND (
                    EXISTS (
                        SELECT 1 FROM agent_jobs j
                        WHERE j.user_id = a.id AND j.state IN ('queued', 'running')
                          AND NOT (j.state = 'running' AND j.cancel_requested)
                    )
                    OR EXISTS (
                        SELECT 1 FROM agent_threads t
                        JOIN agent_thread_sandbox_memberships sm ON sm.thread_id = t.id
                        WHERE t.user_id = a.id AND sm.residency_status <> 'none'
                    )
                  )
                ORDER BY a.id LIMIT $1
                """,
                limit,
            )
        return [row["id"] for row in rows]


__all__ = ["PostgresLocalAccountRepository", "termination_lock_key"]
