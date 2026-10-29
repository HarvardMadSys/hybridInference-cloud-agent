"""What the local-account routes share: mode gates, abuse limits, views and refusals.

Every local-account router uses :class:`LocalAuthRoute`: no answer is cached,
and a refused body is reported by field and rule without repeating its value,
so a password or token never comes back in a 422.
"""

from __future__ import annotations

import contextlib
import ipaddress
import math
from typing import TYPE_CHECKING, Any

from fastapi import (
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse

from cloud_agent.accounts.config import (
    LocalAuthNotConfigured,
    local_mode,
    mail_web_base_url,
    signup_flag_enabled,
)
from cloud_agent.accounts.delivery import DeliveryMailer
from cloud_agent.accounts.mail import AccountMail
from cloud_agent.accounts.passwords import HasherBusy
from cloud_agent.accounts.policy import (
    admin_notify_enabled,
    notify_recipients,
    require_email_verification,
)
from cloud_agent.accounts.sms import configured_sender
from cloud_agent.accounts.tokens import rate_limit_key
from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.api.deps import get_client_ip, get_storage, require_store
from cloud_agent.api.errors import (
    AgentConfigurationError,
    gateway_feature_unavailable,
    local_auth_disabled,
    typed_error,
)
from cloud_agent.application.account_security import (
    AccountChanged,
    ConfirmationMismatch,
    OwnerProofRequired,
    OwnerProofUnavailable,
    SamePassword,
    WrongPassword,
)
from cloud_agent.application.accounts import (
    AccountDeleted,
    AccountSuspended,
    BlockedEmailDomain,
    EmailNotVerified,
    InvalidCredentials,
    InvalidEmail,
    InvalidLoginName,
    InvalidToken,
    LocalAccountFlows,
    WeakPassword,
)
from cloud_agent.application.phone_accounts import (
    InvalidPhone,
    InvalidPhoneCode,
    PhoneCooldown,
    PhoneSendFailed,
    PhoneUnavailable,
)
from cloud_agent.domain.accounts import (
    AccountNotFound,
    AccountTransitionRefused,
    AccountVersionConflict,
    IdentifierTaken,
    SelfDeleteRefused,
    WaitlistNotResubmittable,
)
from cloud_agent.identity import session_cookie_is_secure
from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.accounts import BrowserFull
from cloud_agent.schemas.accounts import AccountEntry, AccountIdentity, WaitlistEntry
from cloud_agent.schemas.session import SessionUserResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from fastapi import BackgroundTasks, Response

    from cloud_agent.accounts.mail import AccountMailer
    from cloud_agent.accounts.sms import SmsSender
    from cloud_agent.agent_access import AgentAccessDecision
    from cloud_agent.domain.accounts import AccountRecord
    from cloud_agent.ports.accounts import LocalAccountRepository

logger = get_logger(__name__)

#: One rule: ``(bucket, key, window seconds, attempts allowed in the window)``.
RateRule = tuple[str, str, int, int]

#: The fixed answer for each refusal a flow raises.
_REFUSALS: dict[type[Exception], tuple[int, str, str | None]] = {
    InvalidEmail: (400, "invalid_email", "Enter a valid email address."),
    InvalidLoginName: (400, "invalid_login_name", None),
    BlockedEmailDomain: (400, "email_domain_blocked", "This email domain is not allowed."),
    WeakPassword: (400, "weak_password", None),
    InvalidCredentials: (401, "invalid_credentials", "Invalid email or password."),
    EmailNotVerified: (
        403,
        "email_not_verified",
        "Email not verified. Please check your email for the verification link.",
    ),
    AccountSuspended: (403, "account_suspended", None),
    AccountDeleted: (403, "account_deleted", "This account has been deleted."),
    InvalidToken: (400, "invalid_token", "This link is invalid or has expired."),
    IdentifierTaken: (409, "identifier_taken", "That cannot be used for this account."),
    SelfDeleteRefused: (403, "self_delete_refused", None),
    BrowserFull: (
        409,
        "browser_full",
        "This browser is already signed in to 5 accounts. Sign one of them out first.",
    ),
    InvalidPhone: (400, "invalid_phone", None),
    PhoneUnavailable: (503, "phone_unavailable", "Phone sign-in is currently unavailable."),
    PhoneSendFailed: (
        503,
        "sms_failed",
        "Could not send the verification code. Please try again later.",
    ),
    InvalidPhoneCode: (400, "invalid_code", "Invalid or expired verification code."),
    WrongPassword: (400, "wrong_password", "Current password is incorrect."),
    OwnerProofRequired: (400, "proof_required", None),
    OwnerProofUnavailable: (
        503,
        "phone_unavailable",
        "Phone verification is currently unavailable. Please contact the administrator.",
    ),
    SamePassword: (400, "same_password", None),
    AccountChanged: (
        409,
        "account_changed",
        "The account changed while this was being saved. Reload and try again.",
    ),
    ConfirmationMismatch: (400, "confirmation_mismatch", None),
    AccountNotFound: (404, "not_found", "No such account."),
    HasherBusy: (503, "unavailable", "Sign-in is busy. Please try again shortly."),
    AccountVersionConflict: (
        409,
        "version_conflict",
        "The account changed since it was read. Reload it and try again.",
    ),
    WaitlistNotResubmittable: (
        409,
        "waitlist_not_resubmittable",
        "This account is already approved.",
    ),
}


class LocalAuthRoute(SensitiveRoute):
    """No caching, input-free validation errors, and the flows' refusals typed."""

    def validation_response(self, exc: Exception) -> Response:
        """Field and rule only; the refused value is never repeated."""
        errors: list[dict[str, Any]] = list(getattr(exc, "errors", list)())
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": list(error.get("loc", ())),
                        "msg": error.get("msg"),
                        "type": error.get("type"),
                    }
                    for error in errors
                ]
            },
        )

    def domain_response(self, exc: Exception) -> Response | None:
        """Translate a flow's refusal into its fixed envelope."""
        if isinstance(exc, AccountTransitionRefused):
            return _envelope(403 if exc.code == "actor_not_admin" else 409, exc.code, str(exc))
        if isinstance(exc, PhoneCooldown):
            response = _envelope(429, "rate_limited", str(exc))
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
        if isinstance(exc, LocalAuthNotConfigured):
            logger.warning(
                "agent_local_auth_unconfigured", extra={"event": "agent_local_auth_unconfigured"}
            )
            error = AgentConfigurationError()
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        for kind, (status, code, message) in _REFUSALS.items():
            if isinstance(exc, kind):
                return _envelope(status, code, message or str(exc))
        return None


def _envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"detail": {"error": {"type": code, "message": message}}}
    )


def require_local_mode() -> None:
    """Refuse a local-account route on a Gateway-mode deployment."""
    if not local_mode():
        raise local_auth_disabled()


def refuse_in_local_mode() -> None:
    """Refuse a Gateway-only reading surface in local mode, before any Gateway call."""
    if local_mode():
        raise gateway_feature_unavailable()


async def _access_ended(store: Any, user_id: str) -> bool:
    from cloud_agent.domain.accounts import permissions_for

    record = await store.local_accounts.get_account(user_id)
    return record is None or "agent.use" not in permissions_for(record)


async def _keep_cleanup_open(store: Any, user_id: str) -> None:
    try:
        await store.local_accounts.reopen_termination(user_id)
    except Exception:
        logger.error(
            "agent_late_terminal_cleanup_unrecorded",
            exc_info=True,
            extra={"event": "agent_late_terminal_cleanup_unrecorded", "user_id": user_id},
        )


@contextlib.asynccontextmanager
async def terminal_open_fence(
    store: Any, user_id: str, manager: Any, workspace_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Fence a terminal opened by a request admitted before its owner's access ended.

    Local mode only, read from this database once the open has finished. The
    caller puts the opened terminal in the yielded dict under ``"terminal"``.

    - Opened, and access has ended: a termination record is reopened first,
      so the enforcement pass kills the group's terminals even if the kill
      below fails, and then the terminal is killed and the request refused.
    - The open failed in a way that leaves its outcome unknown, and access
      has ended: the record is reopened, so a terminal it did create is
      found and killed by the pass.

    A revocation that commits after this check is followed by its own pass,
    which kills every terminal of the group, this one included.

    Raises:
        HTTPException: 403 after reopening the cleanup and killing the terminal.
    """
    opened: dict[str, Any] = {}
    if not local_mode():
        yield opened
        return
    try:
        yield opened
    except BaseException:
        if await _access_ended(store, user_id):
            await _keep_cleanup_open(store, user_id)
        raise
    terminal = opened.get("terminal")
    if terminal is None or not await _access_ended(store, user_id):
        return
    await _keep_cleanup_open(store, user_id)
    with contextlib.suppress(Exception):
        await manager.delete_terminal(workspace_id, str(terminal.get("id") or ""))
    raise HTTPException(
        status_code=403, detail="Agent access has not been granted for this account."
    )


def require_json(request: Request) -> None:
    """Accept only a JSON body: a cross-site form post cannot send one without preflight."""
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise typed_error(415, "unsupported_media_type", "Send this request as application/json.")


def account_mailer(request: Request) -> AccountMailer:
    """The application's mailer: a test's substitute, or the transport the settings select."""
    mailer = getattr(request.app.state, "account_mailer", None)
    if mailer is None:
        mailer = DeliveryMailer()
        request.app.state.account_mailer = mailer
    return mailer


def sms_sender(request: Request) -> SmsSender | None:
    """The application's text message provider: a test's substitute, or the configured one."""
    return getattr(request.app.state, "sms_sender", None) or configured_sender()


def local_accounts(request: Request) -> LocalAccountRepository:
    """The local-account repository, or 503 without a database."""
    return require_store(get_storage(request)).local_accounts


def _web_base_or_none() -> str | None:
    try:
        return mail_web_base_url(allow_http=not session_cookie_is_secure())
    except LocalAuthNotConfigured:
        return None


def mail_available(request: Request) -> bool:
    """Whether this deployment can send account mail: SMTP and a web base for links."""
    return account_mailer(request).configured and _web_base_or_none() is not None


def signup_enabled(request: Request) -> bool:
    """Whether email sign-up is offered: switched on, and able to verify what it creates.

    As HybridInference, sign-up works without SMTP when email verification is
    off: the account is verified at creation and nothing needs mailing.
    """
    return (
        local_mode()
        and signup_flag_enabled()
        and (not require_email_verification() or mail_available(request))
    )


def _admin_notifier(
    request: Request, accounts: LocalAccountRepository, send: Any, base: str
) -> Any:
    async def notify(record: AccountRecord) -> None:
        if not (admin_notify_enabled() and base and account_mailer(request).configured):
            return
        recipients = notify_recipients() or await accounts.admin_emails()
        for recipient in recipients:
            send(
                AccountMail(
                    kind="new_application",
                    to=recipient,
                    link=f"{base}/admin/accounts",
                    details={
                        "account": record.label,
                        "user_name": record.user_name or "",
                        "purpose": record.purpose,
                    },
                )
            )

    return notify


def local_flows(
    request: Request, background: BackgroundTasks | None = None, *, mails: bool = False
) -> LocalAccountFlows:
    """The flows for one request; mail is sent after the response.

    A flow that must mail (``mails``) checks its configuration before touching
    the database, so a misconfigured deployment refuses every address the same
    way. Any other flow mails what it can, a notice or a link, when mail is
    configured, and drops it silently otherwise.
    """
    mailer = account_mailer(request)
    base = _web_base_or_none()
    if mails and (base is None or not mailer.configured):
        raise AgentConfigurationError()

    def send(mail: Any) -> None:
        if background is not None and base is not None and mailer.configured:
            background.add_task(mailer.send, mail)

    accounts = local_accounts(request)
    return LocalAccountFlows(
        accounts,
        web_base_url=lambda: base or "",
        send=send,
        notify_admins=_admin_notifier(request, accounts, send, base or ""),
    )


def client_bucket(request: Request) -> str:
    """The client address as abuse limits key it: IPv6 by its /64."""
    address = get_client_ip(request)
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 6:
        mapped = parsed.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


async def enforce_rate_limits(request: Request, rules: Iterable[RateRule]) -> None:
    """Count one attempt in every rule's window; 429 if any is over.

    Counters live in Postgres, so every worker and replica shares them. A
    store that cannot count refuses the attempt rather than letting it through.
    """
    accounts = local_accounts(request)
    wait = 0.0
    try:
        for bucket, key, window_s, limit in rules:
            wait = max(
                wait,
                await accounts.hit_rate_limit(
                    bucket=bucket,
                    key_hash=rate_limit_key(bucket, key),
                    window_s=window_s,
                    limit=limit,
                ),
            )
    except LocalAuthNotConfigured:
        raise
    except Exception:
        logger.warning(
            "agent_local_rate_limit_unavailable",
            exc_info=True,
            extra={"event": "agent_local_rate_limit_unavailable"},
        )
        raise typed_error(503, "unavailable", "Sign-in is temporarily unavailable.") from None
    if wait > 0:
        raise typed_error(
            429,
            "rate_limited",
            "Too many attempts. Please try again later.",
            **{"Retry-After": str(math.ceil(wait))},
        )


def waitlist_entry(record: AccountRecord) -> WaitlistEntry:
    """The application as its owner and administrators read it."""
    return WaitlistEntry(
        status=record.admission_status,
        purpose=record.purpose,
        review_reason=record.review_reason,
        version=record.version,
        submitted_at=record.submitted_at,
        reviewed_at=record.reviewed_at,
    )


def account_identity(record: AccountRecord) -> AccountIdentity:
    """How an account signs in, as its owner and administrators read it."""
    return AccountIdentity(
        locale=record.locale,
        locale_chosen_at=record.locale_chosen_at,
        email=record.email,
        email_verified=record.email_verified,
        login_name=record.login_name,
        phone=record.phone,
        has_password=record.has_password,
        identity_verified=record.identity_verified,
        user_name=record.user_name,
        label=record.label,
    )


def account_entry(record: AccountRecord) -> AccountEntry:
    """One account for the administration page."""
    return AccountEntry(
        id=record.id,
        email=record.email,
        email_verified=record.email_verified,
        status=record.status,
        is_admin=record.is_admin,
        waitlist=waitlist_entry(record),
        created_at=record.created_at,
        identity=account_identity(record),
        admin_note=record.admin_note,
        suspension_message=record.suspension_message,
        last_login_at=record.last_login_at,
        deleted_at=record.deleted_at,
        erased_at=record.erased_at,
        reviewed_by=record.reviewed_by,
    )


def local_session_user(record: AccountRecord, decision: AgentAccessDecision) -> SessionUserResponse:
    """The session body for a local account: identity, current access and application."""
    return SessionUserResponse(
        id=record.id,
        agent_access=decision.public(),
        auth_mode="local",
        email=record.email,
        waitlist=waitlist_entry(record),
        identity=account_identity(record),
        is_local_admin=record.is_admin,
    )


__all__ = [
    "LocalAuthRoute",
    "RateRule",
    "account_entry",
    "account_identity",
    "account_mailer",
    "client_bucket",
    "enforce_rate_limits",
    "local_accounts",
    "local_flows",
    "local_session_user",
    "mail_available",
    "refuse_in_local_mode",
    "require_json",
    "require_local_mode",
    "signup_enabled",
    "sms_sender",
    "terminal_open_fence",
    "waitlist_entry",
]
