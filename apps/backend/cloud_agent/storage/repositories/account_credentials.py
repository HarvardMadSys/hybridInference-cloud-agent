"""Emailed tokens, passwords and bound identifiers of local accounts.

Every change here locks the account row first (see
:mod:`cloud_agent.storage._sql.local_accounts`) and bumps the account's
version, so an administrator's action read before it is refused as stale.
Identifier uniqueness is the database's: a bind that loses a race to another
account answers :class:`~cloud_agent.domain.accounts.IdentifierTaken` and
changes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.accounts import IdentifierTaken
from cloud_agent.ports.accounts import EmailBinding, PasswordReset
from cloud_agent.storage._sql.local_accounts import (
    BUMP_VERSION_SQL,
    REVOKE_OTHER_SESSIONS_SQL,
    REVOKE_SESSIONS_SQL,
    NotBindable,
    lock_account_row,
    locked_account,
    write_audit,
)

if TYPE_CHECKING:
    from cloud_agent.ports.accounts import EmailTokenPurpose

_CONSUME_ALL_OF_SQL = """
UPDATE agent_local_auth_tokens SET consumed_at = NOW()
WHERE account_id = $1 AND purpose = $2 AND consumed_at IS NULL
"""


async def _upsert_password(conn: Any, account_id: str, password_hash: str) -> None:
    await conn.execute(
        """
        INSERT INTO agent_local_password_credentials (account_id, password_hash)
        VALUES ($1, $2)
        ON CONFLICT (account_id) DO UPDATE
        SET password_hash = EXCLUDED.password_hash, password_changed_at = NOW()
        """,
        account_id,
        password_hash,
    )


async def _clear_claimants_details(conn: Any, account_id: str) -> None:
    """Drop what whoever claimed an unproven address wrote, as its holder takes it back."""
    await conn.execute(
        "UPDATE agent_local_accounts SET user_name = NULL, tos_accepted_at = NULL, "
        "locale = NULL, locale_chosen_at = NULL WHERE id = $1",
        account_id,
    )
    previous = await conn.fetchval(
        "UPDATE agent_local_admissions a SET purpose = '', "
        "status = CASE WHEN a.status = 'pending' THEN 'not_applied' ELSE a.status END, "
        "submitted_at = CASE WHEN a.status = 'pending' THEN NULL ELSE a.submitted_at END "
        "FROM agent_local_admissions old WHERE a.account_id = $1 AND old.account_id = $1 "
        "RETURNING old.status",
        account_id,
    )
    await write_audit(
        conn,
        action="agent.local_account.reclaim",
        actor_id="password-reset",
        admin_ip="local",
        account_id=account_id,
        details={"admission_before": previous},
    )


class CredentialsMixin:
    """Tokens, passwords and identifiers; mixed into the local-account repository."""

    _pool: asyncpg.Pool

    async def issue_email_token(
        self,
        *,
        account_id: str,
        purpose: EmailTokenPurpose,
        token_hash: bytes,
        ttl_s: float,
        pending_email: str | None = None,
        pending_email_normalized: str | None = None,
    ) -> None:
        """Store a new token, consuming every earlier open one of the same purpose.

        A ``bind_email`` token carries the address it will bind, which is what
        a link sent to that address proves.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # The account row orders concurrent issues: the second waits, then
            # consumes the first's token before adding its own.
            await lock_account_row(conn, account_id)
            await conn.execute(_CONSUME_ALL_OF_SQL, account_id, purpose)
            await conn.execute(
                """
                INSERT INTO agent_local_auth_tokens
                    (token_hash, account_id, purpose, expires_at, pending_email,
                     pending_email_normalized)
                VALUES ($1, $2, $3, NOW() + make_interval(secs => $4), $5, $6)
                """,
                token_hash,
                account_id,
                purpose,
                float(ttl_s),
                pending_email,
                pending_email_normalized,
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
        await lock_account_row(conn, account_id)
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
            consumed = await self._consume_token(conn, token_hash, "verify_email")
            if consumed is None:
                return None
            account_id = str(consumed)
            # A link proves the mailbox, also of an address accepted unproven
            # while verification was off.
            changed = await conn.fetchval(
                "UPDATE agent_local_login_identities "
                "SET verified_at = coalesce(verified_at, NOW()), verified_by = 'link' "
                "WHERE account_id = $1 AND kind = 'email' "
                "AND (verified_at IS NULL OR verified_by = 'policy') RETURNING 1",
                account_id,
            )
            if changed:
                await conn.execute(BUMP_VERSION_SQL, account_id)
            return account_id

    async def bind_email(self, *, token_hash: bytes) -> EmailBinding | None:
        """Make a proven address the account's email; ``None`` for an unusable link.

        The address the token carries replaces the current email, verified,
        or becomes the account's first one. Reset links sent to the old
        address are consumed with it: the notice tells that address so.

        Raises:
            IdentifierTaken: Another account signs in with the address now.
                Nothing is consumed: the link stays usable once it is free.
        """
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                consumed = await self._consume_token(conn, token_hash, "bind_email")
                if consumed is None:
                    return None
                account_id = str(consumed)
                row = await conn.fetchrow(
                    "SELECT pending_email, pending_email_normalized FROM agent_local_auth_tokens "
                    "WHERE token_hash = $1",
                    token_hash,
                )
                current = await locked_account(conn, account_id)
                if current.status != "active":
                    # Rolled back with the consumption: the link still works
                    # once the account is restored.
                    raise NotBindable
                previous = current.email
                await conn.execute(
                    """
                    INSERT INTO agent_local_login_identities
                        (account_id, kind, value, value_normalized, verified_at, verified_by)
                    VALUES ($1, 'email', $2, $3, NOW(), 'link')
                    ON CONFLICT (account_id, kind) DO UPDATE
                    SET value = EXCLUDED.value, value_normalized = EXCLUDED.value_normalized,
                        verified_at = NOW(), verified_by = 'link'
                    """,
                    account_id,
                    row["pending_email"],
                    row["pending_email_normalized"],
                )
                await conn.execute(_CONSUME_ALL_OF_SQL, account_id, "reset_password")
                await conn.execute(_CONSUME_ALL_OF_SQL, account_id, "verify_email")
                await conn.execute(BUMP_VERSION_SQL, account_id)
                return EmailBinding(
                    account_id=account_id, previous_email=previous, email=row["pending_email"]
                )
        except asyncpg.UniqueViolationError:
            raise IdentifierTaken from None
        except NotBindable:
            return None

    async def reset_password(
        self, *, token_hash: bytes, password_hash: str
    ) -> PasswordReset | None:
        """Consume a reset token, set the password and end every session.

        The link proves control of the mailbox, so it verifies the email too;
        :attr:`PasswordReset.verified_identity` says whether that made the
        identity verified only now.

        An email accepted unproven while verification was off may have been
        claimed by someone other than its holder. The reset is the holder
        taking the account back (:attr:`PasswordReset.reclaimed`): what the
        claimant wrote goes with the claimant's password and sessions (the
        display name, language, Terms acceptance and application text), a
        pending application returns to not applied for the holder to make,
        and the reclaim is audited. Decisions an administrator already made
        stay, as does the sign-in history, which records the claimant.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            consumed = await self._consume_token(conn, token_hash, "reset_password")
            if consumed is None:
                return None
            account_id = str(consumed)
            await _upsert_password(conn, account_id, password_hash)
            was_verified = await conn.fetchval(
                "SELECT identity_verified FROM agent_local_account_access WHERE account_id = $1",
                account_id,
            )
            reclaimed = (
                await conn.fetchval(
                    "SELECT verified_by FROM agent_local_login_identities "
                    "WHERE account_id = $1 AND kind = 'email' FOR UPDATE",
                    account_id,
                )
                == "policy"
            )
            if reclaimed:
                await _clear_claimants_details(conn, account_id)
            await conn.execute(
                "UPDATE agent_local_login_identities "
                "SET verified_at = coalesce(verified_at, NOW()), verified_by = 'link' "
                "WHERE account_id = $1 AND kind = 'email'",
                account_id,
            )
            await conn.execute(
                "UPDATE agent_local_auth_tokens SET consumed_at = NOW() "
                "WHERE account_id = $1 AND consumed_at IS NULL",
                account_id,
            )
            await conn.execute(REVOKE_SESSIONS_SQL, account_id, "password_reset")
            await conn.execute(BUMP_VERSION_SQL, account_id)
            return PasswordReset(
                account_id=account_id, verified_identity=not was_verified, reclaimed=reclaimed
            )

    async def rehash_password(self, *, account_id: str, old_hash: str, new_hash: str) -> bool:
        """Replace a hash made with older parameters, if it is still the stored one.

        Returns whether this call replaced it. ``False`` means another sign-in
        (or a password change) already did; the caller re-reads the stored hash.
        """
        async with self._pool.acquire() as conn:
            changed = await conn.fetchval(
                "UPDATE agent_local_password_credentials SET password_hash = $3 "
                "WHERE account_id = $1 AND password_hash = $2 RETURNING 1",
                account_id,
                old_hash,
                new_hash,
            )
        return bool(changed)

    async def stored_password_hash(self, account_id: str) -> str | None:
        """The account's current password hash, or ``None`` if it has no password."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT password_hash FROM agent_local_password_credentials WHERE account_id = $1",
                account_id,
            )

    async def change_password(
        self,
        *,
        account_id: str,
        expected_hash: str | None,
        new_hash: str,
        keep_session_id: str | None,
    ) -> bool:
        """Set a new password if the stored one is still the one the owner proved.

        ``expected_hash`` is the hash the current password was verified
        against, or ``None`` when the account had no password (a phone account
        confirming with a code). Every other session ends, as with a reset;
        ``keep_session_id`` stays signed in. Open reset links are consumed:
        each would be another way back in. ``False`` when the password changed
        meanwhile, or the account is no longer active.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await locked_account(conn, account_id)
            if current.status != "active":
                return False
            stored = await conn.fetchval(
                "SELECT password_hash FROM agent_local_password_credentials WHERE account_id = $1",
                account_id,
            )
            if stored != expected_hash:
                return False
            await _upsert_password(conn, account_id, new_hash)
            await conn.execute(
                REVOKE_OTHER_SESSIONS_SQL, account_id, "password_changed", keep_session_id
            )
            await conn.execute(_CONSUME_ALL_OF_SQL, account_id, "reset_password")
            await conn.execute(BUMP_VERSION_SQL, account_id)
            return True

    async def bind_phone(self, *, account_id: str, phone: str) -> bool:
        """Bind a phone number proven by a code, to an active account without one.

        As HybridInference, binding never replaces or merges: an account that
        already has a different number keeps it (``False``), and so does one
        that is not active.

        Raises:
            IdentifierTaken: Another account signs in with the number.
        """
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                current = await locked_account(conn, account_id)
                if current.status != "active":
                    return False
                if current.phone is not None:
                    return current.phone == phone
                await conn.execute(
                    "INSERT INTO agent_local_login_identities "
                    "(account_id, kind, value, value_normalized, verified_at, verified_by) "
                    "VALUES ($1, 'phone', $2, $2, NOW(), 'code')",
                    account_id,
                    phone,
                )
                await conn.execute(BUMP_VERSION_SQL, account_id)
                return True
        except asyncpg.UniqueViolationError:
            raise IdentifierTaken from None

    async def identifier_owner(self, kind: str, normalized: str) -> str | None:
        """The account an identifier belongs to, or ``None``."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT account_id FROM agent_local_login_identities "
                "WHERE kind = $1 AND value_normalized = $2",
                kind,
                normalized,
            )


__all__ = ["CredentialsMixin"]
