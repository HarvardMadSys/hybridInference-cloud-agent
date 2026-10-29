"""Local sign-up, sign-in, email verification and password recovery.

Adapted from HybridInference's ``auth_routes.py`` behaviour at
``321e9a6c457dcc80d35977aaac472d54041204a8`` and, for the features added
since, ``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright
(c) 2026 Harvard SEAS), against this service's own ports. What deliberately
differs:

- **Identity is not admission.** A verified account signs in whatever its
  application says; HybridInference refused sign-in while approval was
  pending. Here the waiting-list page needs a session.
- **No answer says whether an email has an account.** Sign-up, resend and
  forgot-password always answer the same; sign-in says "invalid email or
  password" for an unknown identifier, a wrong password and an account
  without a password alike, and only after the right password says the email
  is unverified or the account suspended or deleted.
- **Signing up again with a registered address changes nothing.** Its owner is
  sent a password-reset link instead, so a stranger who signs up first with
  somebody else's address cannot keep the password: only the mailbox can.
- Every emailed credential and session is stored as a keyed hash, single-use
  where it is a token, and never logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from email_validator import EmailNotValidError, validate_email

from cloud_agent.accounts import tokens
from cloud_agent.accounts.config import (
    RESET_PASSWORD_TTL_S,
    VERIFY_EMAIL_TTL_S,
    local_session_ttl_s,
)
from cloud_agent.accounts.mail import AccountMail
from cloud_agent.accounts.passwords import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from cloud_agent.accounts.policy import (
    POLICY_ACTOR,
    admin_emails,
    approves_automatically,
    is_email_domain_blocked,
    require_email_verification,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.accounts.mail import MailKind
    from cloud_agent.domain.accounts import AccountRecord
    from cloud_agent.ports.accounts import (
        IdentifierKind,
        LocalAccountRepository,
        PasswordLogin,
        SessionOwner,
    )

#: Login names as HybridInference accepts them: 3-32 lowercase letters,
#: digits, ``.``, ``_`` or ``-``, starting with a letter or digit. Never ``@``.
LOGIN_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}")


class InvalidEmail(ValueError):
    """The address does not parse."""


class InvalidLoginName(ValueError):
    """The login name breaks the pattern."""


class BlockedEmailDomain(ValueError):
    """A disposable or reserved mail domain."""


class WeakPassword(ValueError):
    """The password breaks the policy; the message says which rule."""


class InvalidCredentials(Exception):
    """Unknown identifier or wrong password; the client is never told which.

    ``account_id`` is for this service's own sign-in events only: the
    account a wrong password was given for, or ``None`` for no account.
    """

    def __init__(self, account_id: str | None = None) -> None:
        """Remember which account, if any, for the event log."""
        super().__init__("invalid credentials")
        self.account_id = account_id


class _AccountRefusal(Exception):
    """A right proof for an account that cannot sign in; ``account_id`` is for the event log."""

    def __init__(self, message: str = "", *, account_id: str | None = None) -> None:
        """Keep the account the refusal concerns."""
        super().__init__(message)
        self.account_id = account_id


class EmailNotVerified(_AccountRefusal):
    """The right password for an account whose email is unverified."""


class AccountSuspended(_AccountRefusal):
    """The right password for a suspended account; ``message`` is the admin's, if any."""

    def __init__(self, message: str | None = None, *, account_id: str | None = None) -> None:
        """Keep the administrator's message for the sign-in page."""
        super().__init__(message or "This account is suspended.", account_id=account_id)
        self.admin_message = message


class AccountDeleted(_AccountRefusal):
    """The right proof for a deleted account."""


class InvalidToken(Exception):
    """An emailed token that is unknown, expired, used or superseded."""


@dataclass(frozen=True)
class SignedIn:
    """A new session and the account it belongs to. ``token`` goes in the cookie only."""

    token: str = field(repr=False)
    account: AccountRecord
    session_id: str = ""


@dataclass(frozen=True)
class SignUpOutcome:
    """What a sign-up did: the new account, or nothing for a taken identifier."""

    account: AccountRecord | None
    waits_for_approval: bool


@dataclass(frozen=True)
class Redeemed:
    """A redeemed email link: a verification, or a new address bound."""

    account_id: str
    kind: Literal["verified", "email_bound"]
    previous_email: str | None = None
    email: str | None = None


def normalize_email(raw: str) -> tuple[str, str]:
    """Return ``(display, normalized)`` for an address, or raise :class:`InvalidEmail`.

    The normalized form is the lowercased address, which is what makes two
    spellings of one mailbox one account, and the key every abuse counter
    for it uses.
    """
    try:
        valid = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError:
        raise InvalidEmail("Enter a valid email address.") from None
    display = valid.normalized
    return display, display.lower()


def normalize_login_name(raw: str) -> str:
    """A login name, trimmed and lowercased, or :class:`InvalidLoginName`."""
    name = raw.strip().lower()
    if not LOGIN_NAME_PATTERN.fullmatch(name):
        raise InvalidLoginName(
            "A login name is 3-32 letters, digits, '.', '_' or '-', starting with a letter or digit."
        )
    return name


def parse_identifier(raw: str) -> tuple[IdentifierKind, str] | None:
    """What a password sign-in names: an email (it has ``@``) or a login name; else ``None``."""
    try:
        if "@" in raw:
            return "email", normalize_email(raw)[1]
        return "login_name", normalize_login_name(raw)
    except (InvalidEmail, InvalidLoginName):
        return None


def identifier_key(raw: str) -> str:
    """The abuse-counter key of what a client signs in or asks mail for.

    An email is keyed by its normalized address, so every spelling of one
    mailbox shares its windows. Anything else is keyed as typed, trimmed and
    lowercased, as ``raw:<value>``: for a login name that is exactly its
    normalized form, and for input that names nothing it is a key apart from
    every real one. No email key starts with ``raw:``, so the two never meet.
    """
    parsed = parse_identifier(raw)
    if parsed is not None and parsed[0] == "email":
        return parsed[1]
    return f"raw:{raw.strip().lower()}"


def require_strong_password(password: str) -> None:
    """Raise :class:`WeakPassword` with the first rule the password breaks."""
    ok, message = validate_password_strength(password)
    if not ok:
        raise WeakPassword(message or "Password does not meet security requirements")


def refuse_unusable_account(account: AccountRecord) -> None:
    """Refuse a sign-in whose proof was right but whose account cannot sign in.

    Raises:
        AccountDeleted, AccountSuspended, EmailNotVerified: In that order:
            the account's state is reported before an address it cannot use.
    """
    if account.status == "deleted":
        raise AccountDeleted(account_id=account.id)
    if account.status != "active":
        raise AccountSuspended(account.suspension_message, account_id=account.id)
    if not account.identity_verified:
        raise EmailNotVerified(account_id=account.id)


class LocalAccountFlows:
    """The flows, over one repository, a web base for links and a mail sink."""

    def __init__(
        self,
        accounts: LocalAccountRepository,
        *,
        web_base_url: Callable[[], str],
        send: Callable[[AccountMail], None],
        notify_admins: Callable[[AccountRecord], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the store, the link base, where outgoing mail goes and the admin notice."""
        self._accounts = accounts
        self._web_base_url = web_base_url
        self._send = send
        self._notify_admins = notify_admins

    @property
    def accounts(self) -> LocalAccountRepository:
        """The repository the flows run over."""
        return self._accounts

    def link(self, page: str) -> str:
        """An absolute link to a page of the web app."""
        return f"{self._web_base_url()}{page}"

    def send(self, mail: AccountMail) -> None:
        """Queue one message."""
        self._send(mail)

    async def mail_token(
        self,
        account_id: str,
        email: str,
        kind: MailKind,
        *,
        pending_normalized: str | None = None,
    ) -> None:
        """Issue a single-use token and mail its link to ``email``."""
        token = tokens.new_email_token()
        purpose = {
            "verify_email": "verify_email",
            "bind_email": "bind_email",
        }.get(kind, "reset_password")
        await self._accounts.issue_email_token(
            account_id=account_id,
            purpose=purpose,  # type: ignore[arg-type]
            token_hash=tokens.email_token_hash(token),
            ttl_s=RESET_PASSWORD_TTL_S if purpose == "reset_password" else VERIFY_EMAIL_TTL_S,
            pending_email=email if purpose == "bind_email" else None,
            pending_email_normalized=pending_normalized,
        )
        page = "/reset-password" if purpose == "reset_password" else "/verify-email"
        self._send(AccountMail(kind=kind, to=email, link=self.link(f"{page}#token={token}")))

    async def sign_up(
        self,
        email: str,
        password: str,
        *,
        user_name: str | None = None,
        use_case: str | None = None,
        tos_accepted: bool = False,
    ) -> SignUpOutcome:
        """Create an account and mail its verification link, or notify the existing owner.

        ``use_case`` submits the Agent application with the sign-up, as
        HybridInference's registration is its request for approval. Without
        one (``None``) the account applies later from the waiting list.

        Raises:
            InvalidEmail, BlockedEmailDomain, WeakPassword: Refused before
                anything is looked up.
        """
        display, normalized = normalize_email(email)
        if is_email_domain_blocked(normalized):
            raise BlockedEmailDomain("This email domain is not allowed.")
        require_strong_password(password)
        # Hashed before the lookup, so a taken address costs the same time.
        hashed = await hash_password(password)
        verified_now = not require_email_verification()
        account_id = tokens.new_account_id()
        created = await self._accounts.create_account(
            account_id=account_id,
            email=display,
            email_normalized=normalized,
            password_hash=hashed,
            user_name=user_name,
            email_verified=verified_now,
            tos_accepted=tos_accepted,
            purpose=None if use_case is None else use_case.strip(),
        )
        if not created:
            existing = await self._accounts.password_login(normalized)
            if (
                existing is not None
                and existing.account.status != "deleted"
                and existing.account.email
            ):
                await self.mail_token(existing.account.id, existing.account.email, "account_exists")
            return SignUpOutcome(account=None, waits_for_approval=False)
        if not verified_now:
            await self.mail_token(account_id, display, "verify_email")
        return await self.after_verified_identity(account_id, applied=use_case is not None)

    async def after_verified_identity(self, account_id: str, *, applied: bool) -> SignUpOutcome:
        """Apply the approval policy once an identity is (or may be) verified.

        An account the policy admits is approved at once, audited as
        ``signup-policy``. One waiting with an application tells the
        administrators, if they asked to be told, exactly once: when its
        identity becomes verified (at sign-up when it already is, otherwise
        when its link is followed), since only then can it be approved.
        ``applied`` is false for a re-check that must not notify again.
        """
        account = await self._accounts.get_account(account_id)
        if account is None:
            return SignUpOutcome(account=None, waits_for_approval=False)
        if account.identity_verified and await approves_automatically(
            self._accounts, account.email
        ):
            approved = await self._accounts.approve_by_policy(
                account_id=account_id, actor_id=POLICY_ACTOR
            )
            if approved is not None:
                return SignUpOutcome(account=approved, waits_for_approval=False)
        waits = account.admission_status == "pending"
        if waits and applied and account.identity_verified and self._notify_admins is not None:
            await self._notify_admins(account)
        return SignUpOutcome(account=account, waits_for_approval=waits)

    async def sign_in(self, identifier: str, password: str) -> SignedIn:
        """Check the password and open a server-side session.

        ``identifier`` is an email address, or the login name of an account
        created without one.

        Raises:
            InvalidCredentials: Unknown identifier, wrong password, or an
                account with no password at all.
            EmailNotVerified, AccountSuspended, AccountDeleted: The right
                password, an account that cannot sign in.
        """
        parsed = parse_identifier(identifier)
        login = await self._accounts.find_login(*parsed) if parsed is not None else None
        stored_hash = login.password_hash if login is not None else None
        if not await verify_password(password, stored_hash):
            raise InvalidCredentials(login.account.id if login is not None else None)
        assert login is not None and stored_hash is not None
        refuse_unusable_account(login.account)
        stored_hash = await self._current_hash(login, password, stored_hash)
        token = tokens.new_session_token()
        session_id = tokens.new_session_id()
        account = await self._accounts.create_session(
            session_id=session_id,
            account_id=login.account.id,
            token_hash=tokens.session_hash(token),
            ttl_s=local_session_ttl_s(),
            password_hash=stored_hash,
        )
        if account is None:
            # Suspended, or the password changed, between the check and now.
            raise InvalidCredentials(login.account.id)
        return SignedIn(
            token=token, account=await self.promote_if_listed(account), session_id=session_id
        )

    async def promote_if_listed(self, account: AccountRecord) -> AccountRecord:
        """Make an account administrator when its verified email is a configured admin address.

        HybridInference's ``ADMIN_EMAILS`` seed, applied when the account signs
        in. Only a verified address counts, and an address is verified only by
        a link sent to it, so listing one cannot hand the console to whoever
        signs up with it first.
        """
        if not (account.email and account.email_verified) or account.is_admin:
            return account
        if account.email.lower() not in admin_emails():
            return account
        promoted = await self._accounts.promote_listed_admin(
            account_id=account.id, actor_id="admin-emails"
        )
        return promoted or account

    async def _current_hash(self, login: PasswordLogin, password: str, stored_hash: str) -> str:
        """The hash to open the session against, upgrading an outdated one.

        Two sign-ins racing to upgrade the same hash both verified the old
        one; the one whose compare-and-swap loses re-reads the hash the winner
        stored and checks the password against it, rather than presenting a
        hash the row no longer holds and being refused.

        Raises:
            InvalidCredentials: The password changed meanwhile and this one no
                longer matches.
        """
        if not needs_rehash(stored_hash):
            return stored_hash
        new_hash = await hash_password(password)
        if await self._accounts.rehash_password(
            account_id=login.account.id, old_hash=stored_hash, new_hash=new_hash
        ):
            return new_hash
        current = await self._accounts.stored_password_hash(login.account.id)
        if current is None or not await verify_password(password, current):
            raise InvalidCredentials(login.account.id)
        return current

    async def redeem_email_link(self, token: str) -> Redeemed:
        """Redeem a verification or address-binding link. Raises :class:`InvalidToken`.

        A binding link makes its address the account's email and tells the
        address it replaced, as HybridInference's ``verify_email`` does.
        """
        if not tokens.looks_like_email_token(token):
            raise InvalidToken
        token_hash = tokens.email_token_hash(token)
        account_id = await self._accounts.verify_email(token_hash=token_hash)
        if account_id is not None:
            await self.after_verified_identity(account_id, applied=True)
            return Redeemed(account_id=account_id, kind="verified")
        binding = await self._accounts.bind_email(token_hash=token_hash)
        if binding is None:
            # A spent link is refused, never reported on: HybridInference
            # answers "already verified", which says something about the
            # account to whoever holds an old link.
            raise InvalidToken
        previous = binding.previous_email
        if previous and previous.lower() != binding.email.lower():
            self._send(
                AccountMail(kind="email_changed", to=previous, details={"new_email": binding.email})
            )
        await self.after_verified_identity(binding.account_id, applied=False)
        return Redeemed(
            account_id=binding.account_id,
            kind="email_bound",
            previous_email=previous,
            email=binding.email,
        )

    async def verify_email(self, token: str) -> None:
        """Redeem a verification link. Raises :class:`InvalidToken`."""
        await self.redeem_email_link(token)

    async def resend_verification(self, email: str) -> None:
        """Mail a fresh verification link if the address has an unverified account."""
        login = await self._find(email)
        if (
            login is not None
            and login.account.status != "deleted"
            and login.account.email
            and not login.account.email_verified
        ):
            await self.mail_token(login.account.id, login.account.email, "verify_email")

    async def forgot_password(self, email: str) -> None:
        """Mail a reset link if the address has an account that is not deleted."""
        login = await self._find(email)
        if login is not None and login.account.status != "deleted" and login.account.email:
            await self.mail_token(login.account.id, login.account.email, "reset_password")

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
        account_id = await self._accounts.reset_password(
            token_hash=tokens.email_token_hash(token), password_hash=hashed
        )
        if account_id is None:
            raise InvalidToken
        # A reset link proves the mailbox as a verification link does, so the
        # approval policy applies here too (and the administrators hear of an
        # application that only now can be approved).
        await self.after_verified_identity(account_id, applied=True)

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
    "LOGIN_NAME_PATTERN",
    "AccountDeleted",
    "AccountSuspended",
    "BlockedEmailDomain",
    "EmailNotVerified",
    "InvalidCredentials",
    "InvalidEmail",
    "InvalidLoginName",
    "InvalidToken",
    "LocalAccountFlows",
    "Redeemed",
    "SignUpOutcome",
    "SignedIn",
    "WeakPassword",
    "identifier_key",
    "normalize_email",
    "normalize_login_name",
    "parse_identifier",
    "refuse_unusable_account",
    "require_strong_password",
]
