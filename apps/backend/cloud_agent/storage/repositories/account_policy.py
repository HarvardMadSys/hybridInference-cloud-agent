"""Phone codes, sign-in events, the signup domain allowlist and first-run setup.

Phone codes follow HybridInference ``storage/phone_auth.py``: one open code per
(number, purpose, account), reserved inactive before the message is sent and
activated only once the provider accepted it, consumed once, and every check
counted against the code's attempts in the same transaction as the check. The
number and the code are stored only as keyed hashes; the send and verify
quotas are counted in ``agent_local_auth_rate_limits`` by the caller.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.accounts import AccountRecord, IdentifierTaken
from cloud_agent.ports.accounts import SignupDomain
from cloud_agent.storage._sql.advisory import LOCK_KEY_SQL
from cloud_agent.storage._sql.local_accounts import read_account, write_audit

if TYPE_CHECKING:
    import asyncpg

#: The purposes a code may be spent on that belong to a signed-in account,
#: which must still be active when the code is sent and when it is spent.
ACCOUNT_PURPOSES = frozenset({"bind", "delete_account", "bind_email", "set_password"})


async def _owner_eligible(conn: Any, account_id: str | None, purpose: str) -> bool:
    if account_id is None:
        return True
    status = await conn.fetchval(
        "SELECT status FROM agent_local_accounts WHERE id = $1 FOR UPDATE", account_id
    )
    if status is None or status == "deleted":
        return False
    return purpose not in ACCOUNT_PURPOSES or status == "active"


class PolicyMixin:
    """Codes, events, domains and setup; mixed into the local-account repository."""

    _pool: asyncpg.Pool

    # ── Phone codes ──────────────────────────────────────────────────────

    async def reserve_phone_code(
        self,
        *,
        phone_hash: bytes,
        purpose: str,
        account_id: str | None,
        challenge_id: str,
        code_hash: bytes,
        expire_s: int,
        cooldown_s: int,
    ) -> int | None:
        """Reserve an inactive code; seconds to wait if the number is cooling down.

        ``None`` refuses the request outright: the account it names is gone,
        deleted or (for an account purpose) not active. Any earlier open code
        for the same (number, purpose, account) is replaced.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _owner_eligible(conn, account_id, purpose):
                return None
            # One number's sends serialize, whatever the purpose, so two
            # concurrent requests cannot both pass the cooldown.
            await conn.execute(
                f"SELECT pg_advisory_xact_lock({LOCK_KEY_SQL})",
                "cloud-agent:phone-code:" + phone_hash.hex(),
            )
            since = await conn.fetchval(
                "SELECT extract(epoch FROM NOW() - max(created_at)) FROM agent_local_phone_codes "
                "WHERE phone_hash = $1",
                phone_hash,
            )
            if since is not None and float(since) < cooldown_s:
                return max(1, int(cooldown_s - float(since)) + 1)
            await conn.execute(
                "DELETE FROM agent_local_phone_codes WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            await conn.execute(
                """
                INSERT INTO agent_local_phone_codes
                    (phone_hash, purpose, account_key, challenge_id, code_hash, expires_at)
                VALUES ($1, $2, $3, $4, $5, NOW() + make_interval(secs => $6))
                ON CONFLICT (phone_hash, purpose, account_key) DO UPDATE SET
                    challenge_id = EXCLUDED.challenge_id, code_hash = EXCLUDED.code_hash,
                    expires_at = EXCLUDED.expires_at, attempts = 0, active = FALSE,
                    consumed_at = NULL, created_at = NOW()
                """,
                phone_hash,
                purpose,
                account_id or "",
                challenge_id,
                code_hash,
                int(expire_s),
            )
            return 0

    async def activate_phone_code(self, challenge_id: str) -> bool:
        """Activate only the code whose message the provider accepted."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE agent_local_phone_codes SET active = TRUE WHERE challenge_id = $1 "
                "AND expires_at > NOW() AND consumed_at IS NULL RETURNING 1",
                challenge_id,
            )
        return row is not None

    async def invalidate_phone_code(self, challenge_id: str) -> None:
        """Make a code that was not sent, or not eligible, unusable, without touching a newer one.

        The row stays, inactive and spent, so it still counts toward the
        number's cooldown: a request answers the same whether or not the
        number has an account, as HybridInference's attempts table makes it.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_phone_codes SET active = FALSE, consumed_at = NOW() "
                "WHERE challenge_id = $1",
                challenge_id,
            )

    async def consume_phone_code(
        self,
        *,
        phone_hash: bytes,
        purpose: str,
        account_id: str | None,
        code_hash: bytes,
        max_attempts: int,
    ) -> bool:
        """Spend one check of the open code; whether it matched (and is now consumed).

        The attempt counts whether or not it matches, committed with the check.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _owner_eligible(conn, account_id, purpose):
                return False
            row = await conn.fetchrow(
                "SELECT code_hash, active, consumed_at, attempts, expires_at > NOW() AS unexpired "
                "FROM agent_local_phone_codes "
                "WHERE phone_hash = $1 AND purpose = $2 AND account_key = $3 FOR UPDATE",
                phone_hash,
                purpose,
                account_id or "",
            )
            if (
                row is None
                or not row["active"]
                or not row["unexpired"]
                or row["consumed_at"] is not None
                or int(row["attempts"]) >= max_attempts
            ):
                return False
            valid = hmac.compare_digest(bytes(row["code_hash"]), code_hash)
            await conn.execute(
                "UPDATE agent_local_phone_codes SET attempts = attempts + 1, "
                "consumed_at = CASE WHEN $4 THEN NOW() ELSE consumed_at END "
                "WHERE phone_hash = $1 AND purpose = $2 AND account_key = $3",
                phone_hash,
                purpose,
                account_id or "",
                valid,
            )
            return valid

    # ── Audit and recipients ─────────────────────────────────────────────

    async def record_account_audit(
        self,
        *,
        action: str,
        actor_id: str,
        admin_ip: str,
        account_id: str | None,
        details: dict[str, Any],
    ) -> None:
        """One audit row for an account event that is not an administrator action."""
        async with self._pool.acquire() as conn:
            await write_audit(
                conn,
                action=action,
                actor_id=actor_id,
                admin_ip=admin_ip,
                account_id=account_id,
                details=details,
            )

    async def admin_emails(self) -> list[str]:
        """The verified emails of every active local administrator."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT i.value FROM agent_local_accounts a "
                "JOIN agent_local_login_identities i ON i.account_id = a.id "
                "AND i.kind = 'email' AND i.verified_at IS NOT NULL "
                "WHERE a.is_admin AND a.status = 'active' ORDER BY a.created_at"
            )
        return [row["value"] for row in rows]

    # ── Sign-in events ───────────────────────────────────────────────────

    async def record_login_event(
        self,
        *,
        account_id: str | None,
        method: str,
        identifier: str,
        outcome: str,
        failure_reason: str | None,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Record one sign-in attempt. The identifier is as normalized, never a password."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_local_login_events
                    (account_id, method, identifier, outcome, failure_reason, ip, user_agent)
                SELECT $1, $2, $3, $4, $5, $6, $7
                WHERE $1::text IS NULL OR EXISTS (SELECT 1 FROM agent_local_accounts WHERE id = $1)
                """,
                account_id,
                method,
                identifier[:320],
                outcome,
                failure_reason,
                ip[:64],
                (user_agent or None) and user_agent[:512],
            )

    async def login_events(self, *, account_id: str, limit: int) -> list[dict[str, Any]]:
        """An account's most recent sign-in events, newest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT method, outcome, failure_reason, ip, user_agent, created_at "
                "FROM agent_local_login_events WHERE account_id = $1 "
                "ORDER BY created_at DESC, id DESC LIMIT $2",
                account_id,
                limit,
            )
        return [dict(row) for row in rows]

    async def purge_login_events(
        self, *, older_than_days: int | None = None, account_id: str | None = None
    ) -> int:
        """Delete sign-in events by age or for one account; how many were deleted."""
        async with self._pool.acquire() as conn:
            if older_than_days is not None:
                status = await conn.execute(
                    "DELETE FROM agent_local_login_events "
                    "WHERE created_at < NOW() - make_interval(days => $1)",
                    int(older_than_days),
                )
            else:
                status = await conn.execute(
                    "DELETE FROM agent_local_login_events WHERE account_id = $1", account_id
                )
        return int(status.split()[-1])

    # ── Signup domain allowlist ──────────────────────────────────────────

    async def list_signup_domains(self) -> list[SignupDomain]:
        """Every allowed domain, alphabetically."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT domain, is_wildcard, created_at, created_by FROM agent_local_signup_domains "
                "ORDER BY domain, is_wildcard"
            )
        return [SignupDomain(**dict(row)) for row in rows]

    async def add_signup_domain(
        self, *, domain: str, is_wildcard: bool, actor_id: str, admin_ip: str
    ) -> bool:
        """Allow a domain; ``False`` if it is already listed."""
        async with self._pool.acquire() as conn, conn.transaction():
            added = await conn.fetchval(
                "INSERT INTO agent_local_signup_domains (domain, is_wildcard, created_by) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1",
                domain,
                is_wildcard,
                actor_id,
            )
            if added:
                await write_audit(
                    conn,
                    action="agent.local_signup_domain.add",
                    actor_id=actor_id,
                    admin_ip=admin_ip,
                    account_id=None,
                    details={"domain": domain, "wildcard": is_wildcard},
                )
            return bool(added)

    async def remove_signup_domain(
        self, *, domain: str, is_wildcard: bool, actor_id: str, admin_ip: str
    ) -> bool:
        """Remove a domain; ``False`` if it was not listed."""
        async with self._pool.acquire() as conn, conn.transaction():
            removed = await conn.fetchval(
                "DELETE FROM agent_local_signup_domains WHERE domain = $1 AND is_wildcard = $2 "
                "RETURNING 1",
                domain,
                is_wildcard,
            )
            if removed:
                await write_audit(
                    conn,
                    action="agent.local_signup_domain.remove",
                    actor_id=actor_id,
                    admin_ip=admin_ip,
                    account_id=None,
                    details={"domain": domain, "wildcard": is_wildcard},
                )
            return bool(removed)

    async def signup_domain_policy(self, domain: str) -> tuple[bool, bool]:
        """``(allowlist is empty, domain is allowed)`` in one read.

        Exact rows match the domain; wildcard rows match any subdomain of
        theirs, never the bare domain itself.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT NOT EXISTS (SELECT 1 FROM agent_local_signup_domains) AS empty,
                       EXISTS (
                           SELECT 1 FROM agent_local_signup_domains
                           WHERE (NOT is_wildcard AND domain = $1)
                              OR (is_wildcard AND right($1, length(domain) + 1) = '.' || domain)
                       ) AS allowed
                """,
                domain,
            )
        return bool(row["empty"]), bool(row["allowed"])

    # ── First-run setup ──────────────────────────────────────────────────

    async def setup_code(self, candidate: str) -> str | None:
        """The pending setup code, created from ``candidate`` on first sight; ``None`` once done.

        Setup creates the first administrator. A database that already has
        one is set up: the marker is written on sight, so an upgrade, or an
        administrator the operator made with the command line, never shows
        the setup page. Unlike HybridInference, sign-up is not refused while
        setup is pending: a signed-up account is never an administrator here,
        so it cannot take the deployment, and it does not close setup either.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"SELECT pg_advisory_xact_lock({LOCK_KEY_SQL})", "cloud-agent:local-setup"
            )
            row = await conn.fetchrow(
                "SELECT setup_code, completed_at FROM agent_local_setup WHERE singleton"
            )
            has_admin = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agent_local_accounts WHERE is_admin)"
            )
            if row is not None and (row["setup_code"] is None or not has_admin):
                return row["setup_code"]
            if has_admin:
                # An administrator made some other way (the command line, the
                # deployment's token) closes setup as well.
                await conn.execute(
                    "INSERT INTO agent_local_setup (completed_at) VALUES (NOW()) "
                    "ON CONFLICT (singleton) DO UPDATE SET setup_code = NULL, completed_at = NOW()"
                )
                return None
            await conn.execute("INSERT INTO agent_local_setup (setup_code) VALUES ($1)", candidate)
            return candidate

    async def setup_pending(self) -> bool:
        """Whether first-run setup is still open, without writing anything."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT NOT EXISTS (SELECT 1 FROM agent_local_accounts WHERE is_admin) "
                    "AND NOT EXISTS (SELECT 1 FROM agent_local_setup WHERE completed_at IS NOT NULL)"
                )
            )

    async def complete_setup(
        self,
        *,
        account_id: str,
        login_name: str,
        password_hash: str,
        user_name: str | None,
        admin_ip: str,
    ) -> AccountRecord | None:
        """Create the first administrator and close setup, atomically; ``None`` if already done.

        Raises:
            IdentifierTaken: The login name already signs in to an account.

        The account signs in by login name and is verified, approved and an
        administrator. Of two concurrent requests with the right code exactly
        one succeeds: both take the setup lock and re-check it is pending.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"SELECT pg_advisory_xact_lock({LOCK_KEY_SQL})", "cloud-agent:local-setup"
            )
            pending = await conn.fetchval(
                "SELECT setup_code IS NOT NULL FROM agent_local_setup WHERE singleton"
            )
            if not pending or await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agent_local_accounts WHERE is_admin)"
            ):
                return None
            if await conn.fetchval(
                "SELECT 1 FROM agent_local_login_identities "
                "WHERE kind = 'login_name' AND value_normalized = $1",
                login_name,
            ):
                raise IdentifierTaken
            await conn.execute(
                "INSERT INTO agent_local_accounts (id, user_name, is_admin) VALUES ($1, $2, TRUE)",
                account_id,
                user_name,
            )
            await conn.execute(
                "INSERT INTO agent_local_login_identities "
                "(account_id, kind, value, value_normalized, verified_at, verified_by) "
                "VALUES ($1, 'login_name', $2, $2, NOW(), 'operator')",
                account_id,
                login_name,
            )
            await conn.execute(
                "INSERT INTO agent_local_password_credentials (account_id, password_hash) "
                "VALUES ($1, $2)",
                account_id,
                password_hash,
            )
            await conn.execute(
                "INSERT INTO agent_local_admissions (account_id, status, reviewed_at, reviewed_by) "
                "VALUES ($1, 'approved', NOW(), 'first-run-setup')",
                account_id,
            )
            await conn.execute(
                "UPDATE agent_local_setup SET setup_code = NULL, completed_at = NOW() "
                "WHERE singleton"
            )
            await write_audit(
                conn,
                action="agent.local_account.setup_admin",
                actor_id="first-run-setup",
                admin_ip=admin_ip,
                account_id=account_id,
                details={"to": {"status": "active", "admission": "approved", "is_admin": True}},
            )
            return await read_account(conn, account_id)


__all__ = ["ACCOUNT_PURPOSES", "PolicyMixin"]
