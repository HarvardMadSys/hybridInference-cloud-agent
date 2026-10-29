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
#: A phone number as typed, before it is normalized.
_PHONE_MAX_CHARS = 32
_CODE_PATTERN = r"^[0-9]{6}$"
#: HybridInference's display name: 2-50 characters once trimmed.
_USER_NAME = r"^\S(.{0,48}\S)?$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhoneAuthConfig(BaseModel):
    """Phone sign-in as the forms need it."""

    enabled: bool
    default_country_code: str = ""
    allowed_prefixes: list[str] = Field(default_factory=list)


class AuthConfigResponse(BaseModel):
    """Which sign-in this deployment offers. Public; carries no secret."""

    mode: Literal["gateway", "local"]
    signup_enabled: bool


class AuthOptionsResponse(BaseModel):
    """What a local deployment's account pages offer. Public; carries no secret."""

    signup_enabled: bool
    #: Whether account mail can be sent: password reset and email binding need it.
    email_delivery: bool = False
    require_email_verification: bool = True
    phone: PhoneAuthConfig = Field(default_factory=lambda: PhoneAuthConfig(enabled=False))
    turnstile_site_key: str = ""
    terms_url: str = ""
    #: True while the first administrator has not been created.
    setup_required: bool = False
    max_accounts_per_browser: int = 5


class EmailPasswordRequest(_Request):
    """Sign-in, and the original sign-up body."""

    email: str = Field(min_length=1, max_length=_EMAIL_MAX_CHARS)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)


class LoginRequest(_Request):
    """Sign-in with an email or a login name. ``email`` is the older name of the field."""

    identifier: str | None = Field(default=None, min_length=1, max_length=_EMAIL_MAX_CHARS)
    email: str | None = Field(default=None, min_length=1, max_length=_EMAIL_MAX_CHARS)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)

    @property
    def name(self) -> str:
        """What the client signs in with."""
        return self.identifier or self.email or ""


class SignupRequest(_Request):
    """Email sign-up. ``use_case`` present submits the Agent application with it."""

    email: str = Field(min_length=1, max_length=_EMAIL_MAX_CHARS)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)
    user_name: str | None = Field(default=None, min_length=2, max_length=50, pattern=_USER_NAME)
    use_case: str | None = Field(default=None, max_length=MAX_PURPOSE_CHARS)
    accepted_tos: bool = False
    turnstile_token: str | None = Field(default=None, max_length=4096)


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


class PhoneCodeRequest(_Request):
    """Ask for a code for one purpose."""

    phone: str = Field(min_length=1, max_length=_PHONE_MAX_CHARS)
    purpose: Literal["signup", "login", "bind", "delete_account", "bind_email", "set_password"]
    turnstile_token: str | None = Field(default=None, max_length=4096)


class PhoneCodeResponse(BaseModel):
    """The same answer whatever the number; how long until another may be asked for."""

    ok: Literal[True] = True
    retry_after: int


class PhoneVerifyRequest(_Request):
    """A number and the code sent to it: phone sign-in and binding."""

    phone: str = Field(min_length=1, max_length=_PHONE_MAX_CHARS)
    code: str = Field(pattern=_CODE_PATTERN)


class PhoneSignupRequest(PhoneVerifyRequest):
    """Sign up with a phone code."""

    user_name: str | None = Field(default=None, min_length=2, max_length=50, pattern=_USER_NAME)
    use_case: str | None = Field(default=None, max_length=MAX_PURPOSE_CHARS)
    accepted_tos: bool = False


class SignupResponse(BaseModel):
    """A sign-up's answer. The same for a taken address, so it reveals nothing."""

    ok: Literal[True] = True
    requires_approval: bool | None = None


class ChangePasswordRequest(_Request):
    """Change the password (``old_password``), or set a first one (``phone_code``)."""

    old_password: str | None = Field(default=None, min_length=1, max_length=_PASSWORD_MAX_CHARS)
    phone_code: str | None = Field(default=None, pattern=_CODE_PATTERN)
    new_password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)


class BindEmailRequest(_Request):
    """Bind or change the account's email, confirmed as the account signs in."""

    email: str = Field(min_length=1, max_length=_EMAIL_MAX_CHARS)
    password: str | None = Field(default=None, min_length=1, max_length=_PASSWORD_MAX_CHARS)
    phone_code: str | None = Field(default=None, pattern=_CODE_PATTERN)


class BindEmailResponse(BaseModel):
    """Whether a link was sent, or the address is already the account's."""

    email: str
    email_verified: bool


class DeleteAccountRequest(_Request):
    """Delete the signed-in account: its identifier typed out, and its proof."""

    confirm: str = Field(min_length=1, max_length=_EMAIL_MAX_CHARS)
    password: str | None = Field(default=None, min_length=1, max_length=_PASSWORD_MAX_CHARS)
    phone_code: str | None = Field(default=None, pattern=_CODE_PATTERN)


class AccountIdentity(BaseModel):
    """How an account signs in. ``label`` is its email, else login name, else phone."""

    email: str | None
    email_verified: bool
    login_name: str | None
    phone: str | None
    has_password: bool
    identity_verified: bool
    user_name: str | None
    label: str


class BrowserAccountEntry(BaseModel):
    """One account signed in on this browser, for the account chooser. No credential."""

    id: str
    label: str
    user_name: str | None
    active: bool
    signed_in: bool
    agent_access: bool | None


class BrowserAccountsResponse(BaseModel):
    """The accounts signed in on this browser."""

    active_account_id: str | None
    accounts: list[BrowserAccountEntry]
    max_accounts: int


class SwitchAccountRequest(_Request):
    """Make another account signed in on this browser the active one."""

    account_id: str = Field(min_length=1, max_length=64)


class SetupAdminRequest(_Request):
    """First-run setup: the code from the startup log and the administrator to create."""

    setup_code: str = Field(min_length=1, max_length=64)
    login_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)
    display_name: str | None = Field(default=None, min_length=2, max_length=50, pattern=_USER_NAME)


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
    email: str | None
    email_verified: bool
    status: Literal["active", "suspended", "deleted"]
    is_admin: bool
    waitlist: WaitlistEntry
    created_at: datetime
    identity: AccountIdentity | None = None
    admin_note: str | None = None
    suspension_message: str | None = None
    last_login_at: datetime | None = None
    deleted_at: datetime | None = None


class AdminAccountsResponse(BaseModel):
    """One page of accounts, the filtered total and the counts per state."""

    accounts: list[AccountEntry]
    total: int
    counts: dict[str, int] = Field(default_factory=dict)


class AccountActionRequest(_Request):
    """One administrator action, at the version the administrator read."""

    action: Literal[
        "approve",
        "reject",
        "revoke",
        "suspend",
        "restore",
        "grant_admin",
        "revoke_admin",
        "delete",
        "resume",
    ]
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)
    expected_version: int = Field(ge=1, le=2**62)


class AccountNotesRequest(_Request):
    """The administrator's note and the message a suspended account is shown."""

    admin_note: str | None = Field(default=None, max_length=MAX_REASON_CHARS)
    suspension_message: str | None = Field(default=None, max_length=MAX_REASON_CHARS)
    expected_version: int = Field(ge=1, le=2**62)


class LoginEventEntry(BaseModel):
    """One sign-in attempt of an account."""

    method: str
    outcome: str
    failure_reason: str | None
    ip: str
    user_agent: str | None
    created_at: datetime


class LoginEventsResponse(BaseModel):
    """An account's most recent sign-in attempts."""

    events: list[LoginEventEntry]


class PurgedResponse(BaseModel):
    """How many rows a purge deleted."""

    deleted: int


class SignupDomainEntry(BaseModel):
    """One allowed sign-up domain."""

    domain: str
    is_wildcard: bool
    created_at: datetime
    created_by: str


class SignupDomainsResponse(BaseModel):
    """The allowlist and the approval policy it serves."""

    domains: list[SignupDomainEntry]
    approval_policy: str


class SignupDomainRequest(_Request):
    """A domain to allow: ``example.edu``, or ``*.example.edu`` for its subdomains."""

    domain: str = Field(min_length=1, max_length=256)


class EraseAccountRequest(_Request):
    """Permanently erase a deleted account: its id typed out again."""

    confirm: str = Field(min_length=1, max_length=64)


__all__ = [
    "AccountActionRequest",
    "AccountEntry",
    "AccountIdentity",
    "AccountNotesRequest",
    "AdminAccountsResponse",
    "AuthConfigResponse",
    "AuthOptionsResponse",
    "BindEmailRequest",
    "BindEmailResponse",
    "BrowserAccountEntry",
    "BrowserAccountsResponse",
    "ChangePasswordRequest",
    "DeleteAccountRequest",
    "EmailPasswordRequest",
    "EmailRequest",
    "EraseAccountRequest",
    "LoginEventEntry",
    "LoginEventsResponse",
    "LoginRequest",
    "OkResponse",
    "PhoneAuthConfig",
    "PhoneCodeRequest",
    "PhoneCodeResponse",
    "PhoneSignupRequest",
    "PhoneVerifyRequest",
    "PurgedResponse",
    "ResetPasswordRequest",
    "SetupAdminRequest",
    "SignupDomainEntry",
    "SignupDomainRequest",
    "SignupDomainsResponse",
    "SignupRequest",
    "SignupResponse",
    "SwitchAccountRequest",
    "TokenRequest",
    "WaitlistEntry",
    "WaitlistSubmitRequest",
]
