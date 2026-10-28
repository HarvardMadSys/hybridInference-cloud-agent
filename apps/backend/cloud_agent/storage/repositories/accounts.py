"""Postgres implementation of the local-account port.

Lock order: an account row first (``FOR UPDATE`` for an edit, ``FOR SHARE`` for
opening a session), then its admission, login, sessions and tokens. Every
writer that can race another takes the account row, so a suspension and a
sign-in, or a password reset and a sign-in, serialize on it rather than
leaving a session the revocation could not see.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.accounts import (
    ACCESS_GRANTING_ACTIONS,
    AccountNotFound,
    AccountRecord,
    AccountTransitionRefused,
    AccountVersionConflict,
    EmailTaken,
    permissions_for,
    plan_admin_action,
    plan_waitlist_submission,
    require_actor_admin,
)
from cloud_agent.ports.accounts import AdminActionResult, PasswordLogin, SessionOwner
from cloud_agent.storage._sql.advisory import LOCK_KEY_SQL, finish_lock_cleanup

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from cloud_agent.domain.accounts import AdmissionEdit
    from cloud_agent.ports.accounts import EmailTokenPurpose

#: Termination passes one process runs at once. Each holds one connection for
#: its lock and borrows one more at a time, so two leave most of the pool free.
TERMINATION_CONCURRENCY = 2

_ACCOUNT_COLUMNS = """
    a.id, l.email, l.email_verified_at IS NOT NULL AS email_verified, a.status,
    a.is_admin, a.version, a.created_at, m.status AS admission_status, m.purpose,
    m.review_reason, m.submitted_at, m.reviewed_at
"""

_ACCOUNT_FROM = """
    FROM agent_local_accounts a
    JOIN agent_local_password_logins l ON l.account_id = a.id
    JOIN agent_local_admissions m ON m.account_id = a.id
"""

_REVOKE_SESSIONS_SQL = """
UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = $2
WHERE account_id = $1 AND revoked_at IS NULL
"""

_BUMP_VERSION_SQL = """
UPDATE agent_local_accounts SET version = version + 1, updated_at = NOW() WHERE id = $1
"""

#: An account without Agent use, by the same rule ``permissions_for`` applies.
_ACCESS_ENDED_SQL = """
(a.status <> 'active' OR m.status <> 'approved' OR l.email_verified_at IS NULL)
"""


def _record(row: Any) -> AccountRecord:
    return AccountRecord(
        id=row["id"],
        email=row["email"],
        email_verified=row["email_verified"],
        status=row["status"],
        is_admin=row["is_admin"],
        version=int(row["version"]),
        created_at=row["created_at"],
        admission_status=row["admission_status"],
        purpose=row["purpose"],
        review_reason=row["review_reason"],
        submitted_at=row["submitted_at"],
        reviewed_at=row["reviewed_at"],
    )


async def _lock_account_row(conn: Any, account_id: str, *, mode: str = "UPDATE") -> bool:
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


async def _locked_account(conn: Any, account_id: str) -> AccountRecord:
    if not await _lock_account_row(conn, account_id):
        raise AccountNotFound(account_id)
    # A fresh statement under the lock reads every table as of now.
    return await _read_account(conn, account_id)


async def _read_account(conn: Any, account_id: str) -> AccountRecord:
    row = await conn.fetchrow(
        f"SELECT {_ACCOUNT_COLUMNS} {_ACCOUNT_FROM} WHERE a.id = $1", account_id
    )
    if row is None:
        raise AccountNotFound(account_id)
    return _record(row)


async def _lock_target_and_actor(
    conn: Any, account_id: str, actor_account_id: str | None
) -> AccountRecord:
    """Lock the target and the acting administrator in id order; the target's record.

    Id order is the one every two-account writer takes, so two administrators
    acting on each other cannot deadlock. The actor's permission is judged
    under its lock, in this transaction.
    """
    if actor_account_id is None or actor_account_id == account_id:
        record = await _locked_account(conn, account_id)
        if actor_account_id is not None:
            require_actor_admin(record)
        return record
    locked: dict[str, AccountRecord | None] = {}
    for locked_id in sorted((account_id, actor_account_id)):
        try:
            locked[locked_id] = await _locked_account(conn, locked_id)
        except AccountNotFound:
            if locked_id == account_id:
                raise
            locked[locked_id] = None
    require_actor_admin(locked[actor_account_id])
    target = locked[account_id]
    assert target is not None
    return target


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


def _audit_details(record: AccountRecord, edit: AdmissionEdit, version: int) -> dict[str, Any]:
    # States and the version only: no email, password, hash or token.
    return {
        "from": {
            "status": record.status,
            "admission": record.admission_status,
            "is_admin": record.is_admin,
        },
        "to": {
            "status": edit.account_status,
            "admission": edit.admission_status,
            "is_admin": edit.is_admin,
        },
        "version": version,
        "reason_given": edit.review_reason is not None,
    }


class PostgresLocalAccountRepository:
    """Local accounts over the control plane's pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool
        self._termination_slots = asyncio.Semaphore(TERMINATION_CONCURRENCY)

    async def create_account(
        self, *, account_id: str, email: str, email_normalized: str, password_hash: str
    ) -> bool:
        """Create an unverified, not-applied account; ``False`` if the email is taken."""
        try:
            return await self._create_account(
                account_id=account_id,
                email=email,
                email_normalized=email_normalized,
                password_hash=password_hash,
            )
        except EmailTaken:
            return False

    async def _create_account(
        self, *, account_id: str, email: str, email_normalized: str, password_hash: str
    ) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            taken = await conn.fetchval(
                "SELECT 1 FROM agent_local_password_logins WHERE email_normalized = $1",
                email_normalized,
            )
            if taken:
                return False
            await conn.execute("INSERT INTO agent_local_accounts (id) VALUES ($1)", account_id)
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_local_password_logins
                    (account_id, email, email_normalized, password_hash)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (email_normalized) DO NOTHING
                RETURNING account_id
                """,
                account_id,
                email,
                email_normalized,
                password_hash,
            )
            if inserted is None:
                # A concurrent sign-up took the address after the check above.
                # Raising rolls the orphan account row back with the transaction.
                raise EmailTaken
            await conn.execute(
                "INSERT INTO agent_local_admissions (account_id) VALUES ($1)", account_id
            )
            return True

    async def password_login(self, email_normalized: str) -> PasswordLogin | None:
        """Return the account signed in with this email, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_ACCOUNT_COLUMNS}, l.password_hash {_ACCOUNT_FROM} "
                "WHERE l.email_normalized = $1",
                email_normalized,
            )
        if row is None:
            return None
        return PasswordLogin(account=_record(row), password_hash=row["password_hash"])

    async def get_account(self, account_id: str) -> AccountRecord | None:
        """Return one account, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_ACCOUNT_COLUMNS} {_ACCOUNT_FROM} WHERE a.id = $1", account_id
            )
        return _record(row) if row is not None else None

    async def account_emails(self, account_ids: list[str]) -> dict[str, str]:
        """Return the email of each listed account that exists."""
        if not account_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT account_id, email FROM agent_local_password_logins "
                "WHERE account_id = ANY($1::text[])",
                list(account_ids),
            )
        return {row["account_id"]: row["email"] for row in rows}

    async def list_accounts(
        self, *, admission_status: str | None, limit: int, offset: int
    ) -> tuple[list[AccountRecord], int]:
        """Return one page of accounts, newest first, and the filtered total."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_ACCOUNT_COLUMNS} {_ACCOUNT_FROM} "
                "WHERE ($1::text IS NULL OR m.status = $1) "
                "ORDER BY a.created_at DESC, a.id DESC LIMIT $2 OFFSET $3",
                admission_status,
                limit,
                offset,
            )
            total = await conn.fetchval(
                "SELECT count(*) FROM agent_local_admissions WHERE ($1::text IS NULL OR status = $1)",
                admission_status,
            )
        return [_record(row) for row in rows], int(total)

    async def issue_email_token(
        self, *, account_id: str, purpose: EmailTokenPurpose, token_hash: bytes, ttl_s: float
    ) -> None:
        """Store a new token, consuming every earlier open one of the same purpose."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The account row orders concurrent issues: the second waits, then
            # consumes the first's token before adding its own.
            await _lock_account_row(conn, account_id)
            await conn.execute(
                "UPDATE agent_local_auth_tokens SET consumed_at = NOW() "
                "WHERE account_id = $1 AND purpose = $2 AND consumed_at IS NULL",
                account_id,
                purpose,
            )
            await conn.execute(
                """
                INSERT INTO agent_local_auth_tokens (token_hash, account_id, purpose, expires_at)
                VALUES ($1, $2, $3, NOW() + make_interval(secs => $4))
                """,
                token_hash,
                account_id,
                purpose,
                float(ttl_s),
            )
            # Expired tokens are of no use to anyone; a bounded sweep keeps the
            # table from growing with abandoned sign-ups.
            await conn.execute(
                "DELETE FROM agent_local_auth_tokens WHERE token_hash IN ("
                "SELECT token_hash FROM agent_local_auth_tokens "
                "WHERE expires_at < NOW() - INTERVAL '1 day' LIMIT 100)"
            )

    async def _consume_token(
        self, conn: Any, token_hash: bytes, purpose: EmailTokenPurpose
    ) -> str | None:
        # The account row is locked before the token's, the order every other
        # writer takes, so a redemption and a reset cannot deadlock.
        account_id = await conn.fetchval(
            "SELECT account_id FROM agent_local_auth_tokens WHERE token_hash = $1 AND purpose = $2",
            token_hash,
            purpose,
        )
        if account_id is None:
            return None
        await _locked_account(conn, account_id)
        # One UPDATE decides single use: two concurrent redemptions cannot both
        # see the token open.
        return await conn.fetchval(
            """
            UPDATE agent_local_auth_tokens SET consumed_at = NOW()
            WHERE token_hash = $1 AND purpose = $2 AND consumed_at IS NULL
              AND expires_at > NOW()
            RETURNING account_id
            """,
            token_hash,
            purpose,
        )

    async def verify_email(self, *, token_hash: bytes) -> str | None:
        """Consume a verification token and mark its email verified; its account id."""
        async with self._pool.acquire() as conn, conn.transaction():
            account_id = await self._consume_token(conn, token_hash, "verify_email")
            if account_id is None:
                return None
            changed = await conn.fetchval(
                "UPDATE agent_local_password_logins SET email_verified_at = NOW() "
                "WHERE account_id = $1 AND email_verified_at IS NULL RETURNING 1",
                account_id,
            )
            if changed:
                await conn.execute(_BUMP_VERSION_SQL, account_id)
            return str(account_id)

    async def reset_password(self, *, token_hash: bytes, password_hash: str) -> str | None:
        """Consume a reset token, set the password and end every session; its account id."""
        async with self._pool.acquire() as conn, conn.transaction():
            account_id = await self._consume_token(conn, token_hash, "reset_password")
            if account_id is None:
                return None
            # The link proves control of the mailbox, so it verifies the email too.
            await conn.execute(
                """
                UPDATE agent_local_password_logins
                SET password_hash = $2, password_changed_at = NOW(),
                    email_verified_at = COALESCE(email_verified_at, NOW())
                WHERE account_id = $1
                """,
                account_id,
                password_hash,
            )
            await conn.execute(
                "UPDATE agent_local_auth_tokens SET consumed_at = NOW() "
                "WHERE account_id = $1 AND consumed_at IS NULL",
                account_id,
            )
            await conn.execute(_REVOKE_SESSIONS_SQL, account_id, "password_reset")
            await conn.execute(_BUMP_VERSION_SQL, account_id)
            return str(account_id)

    async def rehash_password(self, *, account_id: str, old_hash: str, new_hash: str) -> None:
        """Replace a hash made with older parameters, if it is still the stored one."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_password_logins SET password_hash = $3 "
                "WHERE account_id = $1 AND password_hash = $2",
                account_id,
                old_hash,
                new_hash,
            )

    async def create_session(
        self,
        *,
        session_id: str,
        account_id: str,
        token_hash: bytes,
        ttl_s: float,
        password_hash: str,
    ) -> AccountRecord | None:
        """Open a session if the account may still sign in with that password.

        The account row is share-locked, so a suspension or password reset
        that commits first is seen here, and one that starts later waits for
        this session to exist before it revokes every session.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _lock_account_row(conn, account_id, mode="SHARE"):
                return None
            row = await conn.fetchrow(
                f"SELECT {_ACCOUNT_COLUMNS}, l.password_hash {_ACCOUNT_FROM} WHERE a.id = $1",
                account_id,
            )
            if row is None or row["password_hash"] != password_hash:
                return None
            record = _record(row)
            if not (record.email_verified and record.status == "active"):
                return None
            await conn.execute(
                """
                INSERT INTO agent_local_sessions (id, account_id, token_hash, expires_at)
                VALUES ($1, $2, $3, NOW() + make_interval(secs => $4))
                """,
                session_id,
                account_id,
                token_hash,
                float(ttl_s),
            )
            await conn.execute(
                "DELETE FROM agent_local_sessions WHERE id IN ("
                "SELECT id FROM agent_local_sessions "
                "WHERE expires_at < NOW() - INTERVAL '1 day' LIMIT 100)"
            )
            return record

    async def session_owner(self, token_hash: bytes) -> SessionOwner | None:
        """Return a live session's account, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT s.id AS session_id, {_ACCOUNT_COLUMNS} "
                "FROM agent_local_sessions s "
                "JOIN agent_local_accounts a ON a.id = s.account_id "
                "JOIN agent_local_password_logins l ON l.account_id = a.id "
                "JOIN agent_local_admissions m ON m.account_id = a.id "
                "WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > NOW()",
                token_hash,
            )
        if row is None:
            return None
        return SessionOwner(session_id=row["session_id"], account=_record(row))

    async def revoke_session(self, token_hash: bytes, *, reason: str) -> None:
        """End one session."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = $2 "
                "WHERE token_hash = $1 AND revoked_at IS NULL",
                token_hash,
                reason,
            )

    async def submit_application(self, *, account_id: str, purpose: str) -> AccountRecord:
        """Apply, or resubmit after a rejection or revocation; repeating pending is a no-op."""
        async with self._pool.acquire() as conn, conn.transaction():
            record = await _locked_account(conn, account_id)
            if not plan_waitlist_submission(record):
                return record
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
            await conn.execute(_BUMP_VERSION_SQL, account_id)
            return await _read_account(conn, account_id)

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
            record = await _lock_target_and_actor(conn, account_id, actor_account_id)
            if record.version != expected_version:
                raise AccountVersionConflict(record.version)
            if action in ACCESS_GRANTING_ACTIONS and (
                await _cleanup_open(conn, account_id)
                or not await conn.fetchval(
                    f"SELECT pg_try_advisory_xact_lock({LOCK_KEY_SQL})",
                    termination_lock_key(account_id),
                )
            ):
                raise AccountTransitionRefused(
                    "The account's earlier access is still being stopped; try again shortly.",
                    code="access_cleanup_pending",
                )
            edit = plan_admin_action(
                record, action, reason=reason, actor_account_id=actor_account_id
            )
            await self._write_edit(conn, record, edit, actor_id=actor_id)
            if edit.ends_access:
                await conn.execute(
                    "INSERT INTO agent_local_access_terminations (account_id, generation, action) "
                    "VALUES ($1, $2, $3)",
                    account_id,
                    record.version + 1,
                    action,
                )
            await conn.execute(
                """
                INSERT INTO agent_admin_audit_log
                    (action, actor_id, admin_ip, target_user_id, details, success)
                VALUES ($1, $2, $3, $4, $5::jsonb, TRUE)
                """,
                f"agent.local_account.{action}",
                actor_id,
                admin_ip,
                account_id,
                json.dumps(_audit_details(record, edit, record.version + 1)),
            )
            return AdminActionResult(account=await _read_account(conn, account_id), edit=edit)

    async def _write_edit(
        self, conn: Any, record: AccountRecord, edit: AdmissionEdit, *, actor_id: str
    ) -> None:
        await conn.execute(
            "UPDATE agent_local_accounts SET status = $2, is_admin = $3, "
            "version = version + 1, updated_at = NOW() WHERE id = $1",
            record.id,
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
            record.id,
            edit.admission_status,
            edit.reviewed,
            edit.review_reason,
            actor_id,
        )
        if edit.account_status == "suspended" and record.status == "active":
            await conn.execute(_REVOKE_SESSIONS_SQL, record.id, "suspended")

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
                record = await _locked_account(conn, account_id)
            except AccountNotFound:
                return False
            if permissions_for(record):
                return False
            await conn.execute(
                """
                INSERT INTO agent_local_access_terminations (account_id, generation, action)
                VALUES ($1, $2, $3)
                ON CONFLICT (account_id, generation)
                DO UPDATE SET completed_at = NULL, last_outcome = 'reopened',
                              revision = agent_local_access_terminations.revision + 1
                """,
                account_id,
                record.version,
                "suspend" if record.status == "suspended" else "revoke",
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
                SELECT a.id {_ACCOUNT_FROM}
                WHERE {_ACCESS_ENDED_SQL}
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


__all__ = ["PostgresLocalAccountRepository"]
