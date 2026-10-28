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
    from typing import Any

    from cloud_agent.domain.accounts import AccountRecord, AdmissionEdit

EmailTokenPurpose = Literal["verify_email", "reset_password"]


@dataclass(frozen=True)
class PasswordLogin:
    """An account found by email, with the hash its password is checked against."""

    account: AccountRecord
    password_hash: str = field(repr=False)


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


class LocalAccountRepository(Protocol):
    """Accounts, sign-in credentials, sessions and the waiting list."""

    async def create_account(
        self, *, account_id: str, email: str, email_normalized: str, password_hash: str
    ) -> bool:
        """Create an unverified, not-applied account; ``False`` if the email is taken."""
        ...

    async def password_login(self, email_normalized: str) -> PasswordLogin | None:
        """Return the account signed in with this email, or ``None``."""
        ...

    async def get_account(self, account_id: str) -> AccountRecord | None:
        """Return one account, or ``None``."""
        ...

    async def account_emails(self, account_ids: list[str]) -> dict[str, str]:
        """Return the email of each listed account that exists."""
        ...

    async def list_accounts(
        self, *, admission_status: str | None, limit: int, offset: int
    ) -> tuple[list[AccountRecord], int]:
        """Return one page of accounts, newest first, and the filtered total."""
        ...

    async def issue_email_token(
        self, *, account_id: str, purpose: EmailTokenPurpose, token_hash: bytes, ttl_s: float
    ) -> None:
        """Store a new token, consuming every earlier open one of the same purpose."""
        ...

    async def verify_email(self, *, token_hash: bytes) -> str | None:
        """Consume a verification token and mark its email verified; its account id."""
        ...

    async def reset_password(self, *, token_hash: bytes, password_hash: str) -> str | None:
        """Consume a reset token, set the password and end every session; its account id."""
        ...

    async def rehash_password(self, *, account_id: str, old_hash: str, new_hash: str) -> None:
        """Replace a hash made with older parameters, if it is still the stored one."""
        ...

    async def create_session(
        self,
        *,
        session_id: str,
        account_id: str,
        token_hash: bytes,
        ttl_s: float,
        password_hash: str,
    ) -> AccountRecord | None:
        """Open a session if the account may still sign in with that password."""
        ...

    async def session_owner(self, token_hash: bytes) -> SessionOwner | None:
        """Return a live session's account, or ``None``."""
        ...

    async def revoke_session(self, token_hash: bytes, *, reason: str) -> None:
        """End one session."""
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


class OwnerWorkTermination(Protocol):
    """Stopping an owner's work once their Agent access has ended."""

    async def end_owner_work(self, user_id: str) -> dict[str, Any]:
        """Cancel queued turns, ask running ones to stop and release residencies.

        Returns ``cancelled`` and ``stopping`` job counts and the
        ``released_residencies`` whose grants and processes the caller closes.
        """
        ...


__all__ = [
    "AdminActionResult",
    "EmailTokenPurpose",
    "LocalAccountRepository",
    "OwnerWorkTermination",
    "PasswordLogin",
    "SessionOwner",
]
