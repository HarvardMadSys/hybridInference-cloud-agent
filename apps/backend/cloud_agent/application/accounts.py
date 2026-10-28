"""Local sign-up, sign-in, email verification and password recovery.

Adapted from HybridInference's ``auth_routes.py`` behaviour at
``321e9a6c457dcc80d35977aaac472d54041204a8`` (MIT License, Copyright (c) 2026
Harvard SEAS), against this service's own ports. What deliberately differs:

- **Identity is not admission.** A verified account signs in whatever its
  application says; HybridInference refused sign-in while approval was
  pending. Here the waiting-list page needs a session.
- **No answer says whether an email has an account.** Sign-up, resend and
  forgot-password always answer the same; sign-in says "invalid email or
  password" for both an unknown email and a wrong password, and only after the
  right password says the email is unverified or the account suspended.
- **Signing up again with a registered address changes nothing.** Its owner is
  sent a password-reset link instead, so a stranger who signs up first with
  somebody else's address cannot keep the password: only the mailbox can.
- Every emailed credential and session is stored as a keyed hash, single-use
  where it is a token, and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from email_validator import EmailNotValidError, validate_email

from cloud_agent.accounts import tokens
from cloud_agent.accounts.config import (
    LOCAL_SESSION_TTL_S,
    RESET_PASSWORD_TTL_S,
    VERIFY_EMAIL_TTL_S,
)
from cloud_agent.accounts.mail import AccountMail
from cloud_agent.accounts.passwords import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.accounts.mail import MailKind
    from cloud_agent.domain.accounts import AccountRecord
    from cloud_agent.ports.accounts import LocalAccountRepository, PasswordLogin, SessionOwner


class InvalidEmail(ValueError):
    """The address does not parse."""


class WeakPassword(ValueError):
    """The password breaks the policy; the message says which rule."""


class InvalidCredentials(Exception):
    """Unknown email or wrong password; never says which."""


class EmailNotVerified(Exception):
    """The right password for an account whose email is unverified."""


class AccountSuspended(Exception):
    """The right password for a suspended account."""


class InvalidToken(Exception):
    """An emailed token that is unknown, expired, used or superseded."""


@dataclass(frozen=True)
class SignedIn:
    """A new session and the account it belongs to. ``token`` goes in the cookie only."""

    token: str = field(repr=False)
    account: AccountRecord


def normalize_email(raw: str) -> tuple[str, str]:
    """Return ``(display, normalized)`` for an address, or raise :class:`InvalidEmail`.

    The normalized form is the lowercased address, which is what makes two
    spellings of one mailbox one account.
    """
    try:
        valid = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError:
        raise InvalidEmail("Enter a valid email address.") from None
    display = valid.normalized
    return display, display.lower()


def require_strong_password(password: str) -> None:
    """Raise :class:`WeakPassword` with the first rule the password breaks."""
    ok, message = validate_password_strength(password)
    if not ok:
        raise WeakPassword(message or "Password does not meet security requirements")


class LocalAccountFlows:
    """The flows, over one repository, a web base for links and a mail sink."""

    def __init__(
        self,
        accounts: LocalAccountRepository,
        *,
        web_base_url: Callable[[], str],
        send: Callable[[AccountMail], None],
    ) -> None:
        """Bind the store, the link base and where outgoing mail goes."""
        self._accounts = accounts
        self._web_base_url = web_base_url
        self._send = send

    async def _mail_token(self, account_id: str, email: str, kind: MailKind) -> None:
        token = tokens.new_email_token()
        reset = kind != "verify_email"
        await self._accounts.issue_email_token(
            account_id=account_id,
            purpose="reset_password" if reset else "verify_email",
            token_hash=tokens.email_token_hash(token),
            ttl_s=RESET_PASSWORD_TTL_S if reset else VERIFY_EMAIL_TTL_S,
        )
        page = "/reset-password" if reset else "/verify-email"
        self._send(
            AccountMail(kind=kind, to=email, link=f"{self._web_base_url()}{page}#token={token}")
        )

    async def sign_up(self, email: str, password: str) -> None:
        """Create an account and mail its verification link, or notify the existing owner."""
        display, normalized = normalize_email(email)
        require_strong_password(password)
        # Hashed before the lookup, so a taken address costs the same time.
        hashed = await hash_password(password)
        account_id = tokens.new_account_id()
        created = await self._accounts.create_account(
            account_id=account_id,
            email=display,
            email_normalized=normalized,
            password_hash=hashed,
        )
        if created:
            await self._mail_token(account_id, display, "verify_email")
            return
        existing = await self._accounts.password_login(normalized)
        if existing is not None:
            await self._mail_token(existing.account.id, existing.account.email, "account_exists")

    async def sign_in(self, email: str, password: str) -> SignedIn:
        """Check the password and open a server-side session.

        Raises:
            InvalidCredentials: Unknown email or wrong password.
            EmailNotVerified: Right password, unverified email.
            AccountSuspended: Right password, suspended account.
        """
        try:
            _display, normalized = normalize_email(email)
        except InvalidEmail:
            await verify_password(password, None)
            raise InvalidCredentials from None
        login = await self._accounts.password_login(normalized)
        if not await verify_password(password, login.password_hash if login else None):
            raise InvalidCredentials
        assert login is not None
        if not login.account.email_verified:
            raise EmailNotVerified
        if login.account.status != "active":
            raise AccountSuspended
        stored_hash = login.password_hash
        if needs_rehash(stored_hash):
            new_hash = await hash_password(password)
            await self._accounts.rehash_password(
                account_id=login.account.id, old_hash=stored_hash, new_hash=new_hash
            )
            stored_hash = new_hash
        token = tokens.new_session_token()
        account = await self._accounts.create_session(
            session_id=tokens.new_session_id(),
            account_id=login.account.id,
            token_hash=tokens.session_hash(token),
            ttl_s=LOCAL_SESSION_TTL_S,
            password_hash=stored_hash,
        )
        if account is None:
            # Suspended, or the password changed, between the check and now.
            raise InvalidCredentials
        return SignedIn(token=token, account=account)

    async def verify_email(self, token: str) -> None:
        """Redeem a verification link. Raises :class:`InvalidToken`."""
        if not tokens.looks_like_email_token(token):
            raise InvalidToken
        if await self._accounts.verify_email(token_hash=tokens.email_token_hash(token)) is None:
            raise InvalidToken

    async def resend_verification(self, email: str) -> None:
        """Mail a fresh verification link if the address has an unverified account."""
        login = await self._find(email)
        if login is not None and not login.account.email_verified:
            await self._mail_token(login.account.id, login.account.email, "verify_email")

    async def forgot_password(self, email: str) -> None:
        """Mail a reset link if the address has an account."""
        login = await self._find(email)
        if login is not None:
            await self._mail_token(login.account.id, login.account.email, "reset_password")

    async def reset_password(self, token: str, password: str) -> None:
        """Set a new password from a reset link and end every session.

        Raises:
            WeakPassword: The new password breaks the policy.
            InvalidToken: The link is unknown, expired, used or superseded.
        """
        require_strong_password(password)
        if not tokens.looks_like_email_token(token):
            raise InvalidToken
        hashed = await hash_password(password)
        if (
            await self._accounts.reset_password(
                token_hash=tokens.email_token_hash(token), password_hash=hashed
            )
            is None
        ):
            raise InvalidToken

    async def session_owner(self, token: str | None) -> SessionOwner | None:
        """The live session behind a presented value, or ``None``."""
        if not tokens.looks_like_session_token(token):
            return None
        assert token is not None
        return await self._accounts.session_owner(tokens.session_hash(token))

    async def sign_out(self, token: str | None) -> None:
        """Revoke the presented session, if it is one."""
        if tokens.looks_like_session_token(token):
            assert token is not None
            await self._accounts.revoke_session(tokens.session_hash(token), reason="signed_out")

    async def _find(self, email: str) -> PasswordLogin | None:
        try:
            _display, normalized = normalize_email(email)
        except InvalidEmail:
            return None
        return await self._accounts.password_login(normalized)


__all__ = [
    "AccountSuspended",
    "EmailNotVerified",
    "InvalidCredentials",
    "InvalidEmail",
    "InvalidToken",
    "LocalAccountFlows",
    "SignedIn",
    "WeakPassword",
    "normalize_email",
    "require_strong_password",
]
