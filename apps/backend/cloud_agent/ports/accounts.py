"""Persistence for local accounts, their sessions, emailed tokens and abuse counters.

Only ``AGENT_AUTH_MODE=local`` reaches this port. Every method that changes an
account or its admission bumps the account's one version in the same
transaction, and every administrator change writes its audit row there too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime
    from typing import Any

    from cloud_agent.domain.accounts import AccountRecord, AdmissionEdit

EmailTokenPurpose = Literal["verify_email", "reset_password", "bind_email"]
IdentifierKind = Literal["email", "login_name", "phone"]


@dataclass(frozen=True)
class PasswordLogin:
    """An account found by an identifier, with the hash a password is checked against.

    ``password_hash`` is ``None`` for an account without a password (one
    created with a phone code), which no password signs in to.
    """

    account: AccountRecord
    password_hash: str | None = field(repr=False)


@dataclass(frozen=True)
class SessionOwner:
    """The account a live session belongs to."""

    session_id: str
    account: AccountRecord


@dataclass(frozen=True)
class AdminActionResult:
    """An applied administrator action: the account after it, and what changed."""

    account: AccountRecord
    edit: AdmissionEdit


@dataclass(frozen=True)
class BrowserAccount:
    """One account signed in on a browser, through its own session."""

    account: AccountRecord
    session_id: str
    session_live: bool


@dataclass
class BrowserState:
    """A browser the cookie names: its accounts, oldest first, and the active one."""

    id: str
    active_account_id: str | None
    accounts: list[BrowserAccount]

    def member(self, account_id: str | None) -> BrowserAccount | None:
        """The account's place on this browser, or ``None``."""
        return next((m for m in self.accounts if m.account.id == account_id), None)


class BrowserFull(Exception):
    """The browser already holds as many accounts as one browser may."""

    def __init__(self, limit: int) -> None:
        """Remember the limit the caller reports."""
        super().__init__(f"this browser already holds {limit} accounts")
        self.limit = limit


@dataclass(frozen=True)
class EmailBinding:
    """An address a link proved, now the account's email, and the one it replaced."""

    account_id: str
    previous_email: str | None
    email: str


@dataclass(frozen=True)
class ErasureResult:
    """An erased account, the Agent rows deleted per table, and archives left in the bucket."""

    account: AccountRecord
    deleted: dict[str, int]
    retained_archives: list[str]


@dataclass(frozen=True)
class SignupDomain:
    """One allowlist row. A wildcard is stored without ``*.`` and matches subdomains."""

    domain: str
    is_wildcard: bool
    created_at: datetime
    created_by: str


class AccountBroadcasts(Protocol):
    """Administrator mail to accounts: snapshot, claim and outcomes."""

    async def broadcast_audience(self, *, statuses: list[str], admissions: list[str]) -> int:
        """How many accounts a broadcast to these states would reach now."""
        ...

    async def create_broadcast(
        self,
        *,
        broadcast_id: str,
        subject: str,
        body_html: str,
        body_text: str,
        template_key: str | None,
        statuses: list[str],
        admissions: list[str],
        scheduled_at: datetime | None,
        created_by: str,
        admin_ip: str,
    ) -> int:
        """Create a scheduled broadcast with its recipients fixed now; how many."""
        ...

    async def list_broadcasts(self, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        """Broadcasts, newest first, and how many there are."""
        ...

    async def broadcast_detail(
        self, *, broadcast_id: str, limit: int, offset: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int] | None:
        """One broadcast, a page of its recipients and their total."""
        ...

    async def cancel_broadcast(
        self, *, broadcast_id: str, actor_id: str, admin_ip: str
    ) -> str | None:
        """Cancel a scheduled broadcast; its status after."""
        ...

    async def claim_due_broadcast(self) -> dict[str, Any] | None:
        """Claim one broadcast that is due, or whose sender stopped."""
        ...

    async def pending_broadcast_recipients(
        self, *, broadcast_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """The next pending recipients, with whether each may still be mailed."""
        ...

    async def record_broadcast_results(
        self, *, broadcast_id: str, outcomes: list[tuple[str, str, str | None]]
    ) -> None:
        """Record each handled recipient's outcome."""
        ...

    async def finish_broadcast(self, *, broadcast_id: str) -> str:
        """Close a fully handled broadcast; its final status."""
        ...


class LocalAccountRepository(AccountBroadcasts, Protocol):
    """Accounts, sign-in credentials, sessions, browsers and the waiting list."""

    async def create_account(
        self,
        *,
        account_id: str,
        email: str,
        email_normalized: str,
        password_hash: str,
        user_name: str | None = None,
        email_verified: bool = False,
        tos_accepted: bool = False,
        purpose: str | None = None,
    ) -> bool:
        """Create an email-and-password account; ``False`` if the email is taken."""
        ...

    async def create_login_name_account(
        self, *, account_id: str, login_name: str, password_hash: str, user_name: str | None = None
    ) -> bool:
        """Create an account that signs in by login name; ``False`` if the name is taken."""
        ...

    async def create_phone_account(
        self,
        *,
        account_id: str,
        phone: str,
        user_name: str | None,
        tos_accepted: bool,
        purpose: str | None,
    ) -> bool:
        """Create an account proven by a phone code; ``False`` if the number is taken."""
        ...

    async def password_login(self, email_normalized: str) -> PasswordLogin | None:
        """Return the account signed in with this email, or ``None``."""
        ...

    async def find_login(self, kind: IdentifierKind, normalized: str) -> PasswordLogin | None:
        """The account an identifier signs in to, or ``None``."""
        ...

    async def get_account(self, account_id: str) -> AccountRecord | None:
        """Return one account, or ``None``."""
        ...

    async def account_emails(self, account_ids: list[str]) -> dict[str, str]:
        """Return the email of each listed account that has one."""
        ...

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
        """Return one page of accounts and the filtered total."""
        ...

    async def promote_listed_admin(self, *, account_id: str, actor_id: str) -> AccountRecord | None:
        """Make an account with a configured admin email an administrator."""
        ...

    async def set_locale(
        self, *, account_id: str, locale: str, chosen_at: datetime
    ) -> tuple[str | None, datetime | None]:
        """Store the interface language if it is the newest choice; the one kept."""
        ...

    async def account_counts(self) -> dict[str, int]:
        """Accounts per ``status:<s>`` and ``admission:<s>``, and ``all``."""
        ...

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
        """Store a new token, consuming every earlier open one of the same purpose."""
        ...

    async def verify_email(self, *, token_hash: bytes) -> str | None:
        """Consume a verification token and mark its email verified; its account id."""
        ...

    async def bind_email(self, *, token_hash: bytes) -> EmailBinding | None:
        """Consume a binding token and make its address the account's email."""
        ...

    async def reset_password(self, *, token_hash: bytes, password_hash: str) -> str | None:
        """Consume a reset token, set the password and end every session; its account id."""
        ...

    async def rehash_password(self, *, account_id: str, old_hash: str, new_hash: str) -> bool:
        """Replace an outdated hash if it is still the stored one; whether this call did."""
        ...

    async def stored_password_hash(self, account_id: str) -> str | None:
        """The account's current password hash, or ``None``."""
        ...

    async def change_password(
        self,
        *,
        account_id: str,
        expected_hash: str | None,
        new_hash: str,
        keep_session_id: str | None,
    ) -> bool:
        """Set a new password at the hash the owner proved; end every other session."""
        ...

    async def bind_phone(self, *, account_id: str, phone: str) -> bool:
        """Bind a proven phone number to an active account that has none."""
        ...

    async def identifier_owner(self, kind: str, normalized: str) -> str | None:
        """The account an identifier belongs to, or ``None``."""
        ...

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
        """Open a session if the account may still sign in with the proof it gave."""
        ...

    async def session_owner(self, token_hash: bytes) -> SessionOwner | None:
        """Return a live session's account, or ``None``."""
        ...

    async def revoke_session(self, token_hash: bytes, *, reason: str) -> None:
        """End one session."""
        ...

    async def browser_state(self, token_hash: bytes) -> BrowserState | None:
        """The live browser a cookie names, with its accounts."""
        ...

    async def create_browser(self, *, browser_id: str, token_hash: bytes, ttl_s: float) -> None:
        """Start a browser with no account yet."""
        ...

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
        """Add a session to a live browser, make it active and rotate the cookie value."""
        ...

    async def set_browser_active(self, *, browser_id: str, account_id: str) -> bool:
        """Make a signed-in account active; ``False`` if it is not on the browser."""
        ...

    async def detach_browser_account(
        self, *, browser_id: str, account_id: str, reason: str = "signed_out"
    ) -> int | None:
        """Sign one account out of a browser; how many remain."""
        ...

    async def revoke_browser(self, *, browser_id: str) -> None:
        """Sign every account out of a browser and end it."""
        ...

    async def submit_application(self, *, account_id: str, purpose: str) -> AccountRecord:
        """Apply, or resubmit after a rejection or revocation; repeating pending is a no-op."""
        ...

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
        ...

    async def approve_by_policy(self, *, account_id: str, actor_id: str) -> AccountRecord | None:
        """Approve an account the signup policy admits, if it still may be."""
        ...

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
        """Set the administrator's note and the suspension message."""
        ...

    async def delete_own_account(
        self, *, account_id: str, expected_version: int, admin_ip: str
    ) -> AccountRecord:
        """Delete the caller's account at the version its owner confirmed."""
        ...

    async def erase_account(
        self, *, account_id: str, actor_id: str, actor_account_id: str | None, admin_ip: str
    ) -> ErasureResult:
        """Erase a deleted, stopped account and all its Agent data."""
        ...

    async def hit_rate_limit(
        self, *, bucket: str, key_hash: bytes, window_s: int, limit: int
    ) -> float:
        """Count one attempt; seconds until the window reopens if over ``limit``, else 0."""
        ...

    def termination_lock(self, account_id: str) -> AbstractAsyncContextManager[bool]:
        """Hold the account's termination lock for one pass; yields ``False`` if taken."""
        ...

    async def reopen_termination(self, account_id: str) -> bool:
        """Open a termination at the current version if the account has no access."""
        ...

    async def open_terminations(self, *, limit: int) -> list[tuple[str, int]]:
        """Revocations whose cleanup is not yet acknowledged: ``(account, generation)``."""
        ...

    async def termination_open(self, account_id: str) -> bool:
        """Whether a termination of the account still waits for its cleanup."""
        ...

    async def termination_revision(self, *, account_id: str, generation: int) -> int | None:
        """The record's current revision, read by a pass before it starts its cleanup."""
        ...

    async def record_termination_attempt(
        self, *, account_id: str, generation: int, revision: int, complete: bool, outcome: str
    ) -> bool:
        """Count one attempt; close the record only at ``revision``. Whether it closed."""
        ...

    async def accounts_with_ended_access(self, *, limit: int) -> list[str]:
        """Accounts without Agent use that still have queued, running or resident work."""
        ...

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
        """Reserve an inactive code; seconds to wait, or ``None`` when refused."""
        ...

    async def activate_phone_code(self, challenge_id: str) -> bool:
        """Activate the code the provider accepted."""
        ...

    async def invalidate_phone_code(self, challenge_id: str) -> None:
        """Drop a code whose message was not sent."""
        ...

    async def consume_phone_code(
        self,
        *,
        phone_hash: bytes,
        purpose: str,
        account_id: str | None,
        code_hash: bytes,
        max_attempts: int,
    ) -> bool:
        """Spend one check of the open code; whether it matched."""
        ...

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
        ...

    async def admin_emails(self) -> list[str]:
        """The verified emails of every active local administrator."""
        ...

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
        """Record one sign-in attempt."""
        ...

    async def login_events(self, *, account_id: str, limit: int) -> list[dict[str, Any]]:
        """An account's most recent sign-in events."""
        ...

    async def purge_login_events(
        self, *, older_than_days: int | None = None, account_id: str | None = None
    ) -> int:
        """Delete sign-in events by age or for one account."""
        ...

    async def list_signup_domains(self) -> list[SignupDomain]:
        """Every allowed signup domain."""
        ...

    async def add_signup_domain(
        self, *, domain: str, is_wildcard: bool, actor_id: str, admin_ip: str
    ) -> bool:
        """Allow a domain; ``False`` if listed already."""
        ...

    async def remove_signup_domain(
        self, *, domain: str, is_wildcard: bool, actor_id: str, admin_ip: str
    ) -> bool:
        """Remove a domain; ``False`` if it was not listed."""
        ...

    async def signup_domain_policy(self, domain: str) -> tuple[bool, bool]:
        """``(allowlist empty, domain allowed)``."""
        ...

    async def setup_code(self, candidate: str) -> str | None:
        """The pending setup code, or ``None`` once setup is complete."""
        ...

    async def complete_setup(
        self,
        *,
        account_id: str,
        login_name: str,
        password_hash: str,
        user_name: str | None,
        admin_ip: str,
    ) -> AccountRecord | None:
        """Create the first administrator and close setup; ``None`` if already done."""
        ...


class OwnerWorkTermination(Protocol):
    """Stopping an owner's work once their Agent access has ended."""

    async def end_owner_work(self, user_id: str) -> dict[str, Any]:
        """Cancel queued turns, ask running ones to stop and release residencies.

        Returns ``cancelled`` and ``stopping`` job counts and the
        ``released_residencies`` whose grants and processes the caller closes.
        """
        ...


__all__ = [
    "AccountBroadcasts",
    "AdminActionResult",
    "BrowserAccount",
    "BrowserFull",
    "BrowserState",
    "EmailBinding",
    "EmailTokenPurpose",
    "ErasureResult",
    "IdentifierKind",
    "LocalAccountRepository",
    "OwnerWorkTermination",
    "PasswordLogin",
    "SessionOwner",
    "SignupDomain",
]
