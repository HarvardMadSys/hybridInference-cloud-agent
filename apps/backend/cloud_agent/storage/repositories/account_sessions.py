"""Local sessions, and browsers that hold several signed-in accounts.

Each signed-in account has its own session row, which a password change,
suspension or deletion revokes exactly as before. A *browser* (HybridInference
``auth/browser_sessions.py``) is what the cookie names now: the accounts signed
in on it, each through its own session, and which one is active. Membership
changes lock the browser row, so a sign-in in one tab and a sign-out in another
cannot leave the browser's account count or its active account wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.accounts import may_sign_in
from cloud_agent.ports.accounts import BrowserAccount, BrowserFull, BrowserState, SessionOwner
from cloud_agent.storage._sql.local_accounts import (
    ACCOUNT_COLUMNS,
    ACCOUNT_FROM,
    lock_account_row,
    record,
)

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.domain.accounts import AccountRecord

_SESSION_LIVE = "(s.revoked_at IS NULL AND s.expires_at > NOW())"


async def _lock_browser(conn: Any, browser_id: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM agent_local_browser_sessions "
            "WHERE id = $1 AND revoked_at IS NULL AND expires_at > NOW() FOR UPDATE",
            browser_id,
        )
    )


class SessionsMixin:
    """Sessions and browsers; mixed into the local-account repository."""

    _pool: asyncpg.Pool

    async def create_session(
        self,
        *,
        session_id: str,
        account_id: str,
        token_hash: bytes,
        ttl_s: float,
        password_hash: str | None = None,
        phone: str | None = None,
    ) -> AccountRecord | None:
        """Open a session if the account may still sign in with the proof it gave.

        The proof is the password hash the sign-in verified (still the stored
        one), or the phone number whose code it spent (still the account's).
        The account row is share-locked, so a suspension, deletion or password
        change that commits first is seen here, and one that starts later
        waits for this session to exist before it revokes every session.
        """
        if (password_hash is None) == (phone is None):
            return None
        async with self._pool.acquire() as conn, conn.transaction():
            if not await lock_account_row(conn, account_id, mode="SHARE"):
                return None
            row = await conn.fetchrow(
                f"SELECT {ACCOUNT_COLUMNS}, c.password_hash {ACCOUNT_FROM} WHERE a.id = $1",
                account_id,
            )
            if row is None:
                return None
            if password_hash is not None and row["password_hash"] != password_hash:
                return None
            if phone is not None and row["phone"] != phone:
                return None
            account = record(row)
            if not may_sign_in(account):
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
        # Outside the share-locked transaction: two sign-ins updating the row
        # under their share locks would deadlock each other.
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_accounts SET last_login_at = NOW() WHERE id = $1", account_id
            )
        return account

    async def session_owner(self, token_hash: bytes) -> SessionOwner | None:
        """Return a live session's account, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT s.id AS session_id, {ACCOUNT_COLUMNS} "
                f"{ACCOUNT_FROM} JOIN agent_local_sessions s ON s.account_id = a.id "
                f"WHERE s.token_hash = $1 AND {_SESSION_LIVE}",
                token_hash,
            )
        if row is None:
            return None
        return SessionOwner(session_id=row["session_id"], account=record(row))

    async def revoke_session(self, token_hash: bytes, *, reason: str) -> None:
        """End one session."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = $2 "
                "WHERE token_hash = $1 AND revoked_at IS NULL",
                token_hash,
                reason,
            )

    # ── Browsers ─────────────────────────────────────────────────────────

    async def browser_state(self, token_hash: bytes) -> BrowserState | None:
        """The live browser a cookie names, with every account signed in on it."""
        async with self._pool.acquire() as conn:
            browser = await conn.fetchrow(
                "SELECT id, active_account_id FROM agent_local_browser_sessions "
                "WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > NOW()",
                token_hash,
            )
            if browser is None:
                return None
            rows = await conn.fetch(
                f"SELECT b.session_id, {_SESSION_LIVE} AS live, {ACCOUNT_COLUMNS} "
                f"{ACCOUNT_FROM} JOIN agent_local_browser_accounts b ON b.account_id = a.id "
                "JOIN agent_local_sessions s ON s.id = b.session_id "
                "WHERE b.browser_id = $1 ORDER BY b.added_at, a.id",
                browser["id"],
            )
        return BrowserState(
            id=browser["id"],
            active_account_id=browser["active_account_id"],
            accounts=[
                BrowserAccount(
                    account=record(row), session_id=row["session_id"], session_live=row["live"]
                )
                for row in rows
            ],
        )

    async def create_browser(self, *, browser_id: str, token_hash: bytes, ttl_s: float) -> None:
        """Start a browser with no account yet."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_local_browser_sessions (id, token_hash, expires_at) "
                "VALUES ($1, $2, NOW() + make_interval(secs => $3))",
                browser_id,
                token_hash,
                float(ttl_s),
            )
            await conn.execute(
                "DELETE FROM agent_local_browser_sessions WHERE id IN ("
                "SELECT id FROM agent_local_browser_sessions "
                "WHERE expires_at < NOW() - INTERVAL '1 day' LIMIT 100)"
            )

    async def attach_browser_account(
        self,
        *,
        browser_id: str,
        account_id: str,
        session_id: str,
        max_accounts: int,
        new_token_hash: bytes,
        ttl_s: float,
    ) -> bool:
        """Add a fresh session to a live browser, make its account active and rotate the cookie.

        A session the account already held here is revoked: its place is
        taken. ``False`` when the browser has ended meanwhile; the caller then
        starts a new one.

        Raises:
            BrowserFull: The browser already holds ``max_accounts`` other accounts.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # The account first, then the browser, then sessions: the order a
            # deletion takes, so the two cannot deadlock.
            if not await lock_account_row(conn, account_id, mode="SHARE"):
                return False
            if not await _lock_browser(conn, browser_id):
                return False
            others = await conn.fetchval(
                "SELECT count(*) FROM agent_local_browser_accounts "
                "WHERE browser_id = $1 AND account_id <> $2",
                browser_id,
                account_id,
            )
            if int(others) >= max_accounts:
                raise BrowserFull(max_accounts)
            await conn.execute(
                "UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = 'replaced' "
                "WHERE revoked_at IS NULL AND id = (SELECT session_id FROM "
                "agent_local_browser_accounts WHERE browser_id = $1 AND account_id = $2)",
                browser_id,
                account_id,
            )
            await conn.execute(
                """
                INSERT INTO agent_local_browser_accounts (browser_id, account_id, session_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (browser_id, account_id)
                DO UPDATE SET session_id = EXCLUDED.session_id, added_at = NOW()
                """,
                browser_id,
                account_id,
                session_id,
            )
            # What the browser can act as just changed, so its cookie does too;
            # a value planted before this sign-in no longer names it.
            await conn.execute(
                "UPDATE agent_local_browser_sessions SET active_account_id = $2, token_hash = $3, "
                "expires_at = GREATEST(expires_at, NOW() + make_interval(secs => $4)) "
                "WHERE id = $1",
                browser_id,
                account_id,
                new_token_hash,
                float(ttl_s),
            )
            return True

    async def set_browser_active(self, *, browser_id: str, account_id: str) -> bool:
        """Make a signed-in account the browser's active one; ``False`` if it is not here."""
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _lock_browser(conn, browser_id):
                return False
            changed = await conn.fetchval(
                "UPDATE agent_local_browser_sessions SET active_account_id = $2 "
                "WHERE id = $1 AND EXISTS (SELECT 1 FROM agent_local_browser_accounts "
                "WHERE browser_id = $1 AND account_id = $2) RETURNING 1",
                browser_id,
                account_id,
            )
            return bool(changed)

    async def detach_browser_account(
        self, *, browser_id: str, account_id: str, reason: str = "signed_out"
    ) -> int | None:
        """Sign one account out of a browser; how many remain, or ``None`` if it was not here.

        The browser itself ends with its last account, in the same
        transaction, so a sign-in in another tab cannot land in between.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _lock_browser(conn, browser_id):
                return None
            session_id = await conn.fetchval(
                "DELETE FROM agent_local_browser_accounts WHERE browser_id = $1 AND account_id = $2 "
                "RETURNING session_id",
                browser_id,
                account_id,
            )
            if session_id is None:
                return None
            await conn.execute(
                "UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = $2 "
                "WHERE id = $1 AND revoked_at IS NULL",
                session_id,
                reason,
            )
            remaining = int(
                await conn.fetchval(
                    "SELECT count(*) FROM agent_local_browser_accounts WHERE browser_id = $1",
                    browser_id,
                )
            )
            await conn.execute(
                "UPDATE agent_local_browser_sessions SET "
                "active_account_id = CASE WHEN active_account_id = $2 THEN NULL "
                "ELSE active_account_id END, "
                "revoked_at = CASE WHEN $3 = 0 THEN NOW() ELSE revoked_at END WHERE id = $1",
                browser_id,
                account_id,
                remaining,
            )
            return remaining

    async def revoke_browser(self, *, browser_id: str) -> None:
        """Sign every account out of a browser and end it."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_local_sessions SET revoked_at = NOW(), revoke_reason = 'signed_out' "
                "WHERE revoked_at IS NULL AND id IN (SELECT session_id FROM "
                "agent_local_browser_accounts WHERE browser_id = $1)",
                browser_id,
            )
            await conn.execute(
                "DELETE FROM agent_local_browser_accounts WHERE browser_id = $1", browser_id
            )
            await conn.execute(
                "UPDATE agent_local_browser_sessions SET revoked_at = NOW(), "
                "active_account_id = NULL WHERE id = $1 AND revoked_at IS NULL",
                browser_id,
            )


__all__ = ["SessionsMixin"]
