"""Wire models for local accounts, the waiting list and their administration.

Only ``AGENT_AUTH_MODE=local`` serves these, apart from ``AuthConfigResponse``.
Request models forbid unknown fields: a body cannot carry a status, an admin
flag or a version the route did not ask for.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.domain.accounts import MAX_PURPOSE_CHARS, MAX_REASON_CHARS

#: Longer than any address RFC 5321 allows; the validator decides the rest.
_EMAIL_MAX_CHARS = 320
#: Bounds hashing work before the policy runs.
_PASSWORD_MAX_CHARS = 1024
#: An emailed token is 43 characters; anything far longer is not one.
_TOKEN_MAX_CHARS = 128


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthConfigResponse(BaseModel):
    """Which sign-in this deployment offers. Public; carries no secret."""

    mode: Literal["gateway", "local"]
    signup_enabled: bool


class EmailPasswordRequest(_Request):
    """Sign-up and sign-in."""

    email: str = Field(min_length=1, max_length=_EMAIL_MAX_CHARS)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)


class EmailRequest(_Request):
    """Resend verification, and forgot password."""

    email: str = Field(min_length=1, max_length=_EMAIL_MAX_CHARS)


class TokenRequest(_Request):
    """Verify an email from its link."""

    token: str = Field(min_length=1, max_length=_TOKEN_MAX_CHARS)


class ResetPasswordRequest(_Request):
    """Choose a new password from a reset link."""

    token: str = Field(min_length=1, max_length=_TOKEN_MAX_CHARS)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)


class OkResponse(BaseModel):
    """The same answer whatever happened, where saying more would leak."""

    ok: Literal[True] = True


class WaitlistEntry(BaseModel):
    """An account's Agent application and its review.

    ``version`` is the account's one edit version, the value an
    administrator's ``expected_version`` must match.
    """

    status: Literal["not_applied", "pending", "approved", "rejected", "revoked"]
    purpose: str
    review_reason: str | None
    version: int
    submitted_at: datetime | None
    reviewed_at: datetime | None


class WaitlistSubmitRequest(_Request):
    """Apply for Agent use, or apply again after a rejection or revocation."""

    purpose: str = Field(default="", max_length=MAX_PURPOSE_CHARS)


class AccountEntry(BaseModel):
    """One account as the administration page lists it."""

    id: str
    email: str
    email_verified: bool
    status: Literal["active", "suspended"]
    is_admin: bool
    waitlist: WaitlistEntry
    created_at: datetime


class AdminAccountsResponse(BaseModel):
    """One page of accounts and the filtered total."""

    accounts: list[AccountEntry]
    total: int


class AccountActionRequest(_Request):
    """One administrator action, at the version the administrator read."""

    action: Literal[
        "approve", "reject", "revoke", "suspend", "restore", "grant_admin", "revoke_admin"
    ]
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)
    expected_version: int = Field(ge=1, le=2**62)


__all__ = [
    "AccountActionRequest",
    "AccountEntry",
    "AdminAccountsResponse",
    "AuthConfigResponse",
    "EmailPasswordRequest",
    "EmailRequest",
    "OkResponse",
    "ResetPasswordRequest",
    "TokenRequest",
    "WaitlistEntry",
    "WaitlistSubmitRequest",
]
