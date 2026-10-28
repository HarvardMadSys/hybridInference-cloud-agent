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
from cloud_agent.accounts.mail import SmtpAccountMailer
from cloud_agent.accounts.passwords import HasherBusy
from cloud_agent.accounts.tokens import rate_limit_key
from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.api.deps import get_client_ip, get_storage, require_store
from cloud_agent.api.errors import (
    AgentConfigurationError,
    gateway_feature_unavailable,
    local_auth_disabled,
    typed_error,
)
from cloud_agent.application.accounts import (
    AccountSuspended,
    EmailNotVerified,
    InvalidCredentials,
    InvalidEmail,
    InvalidToken,
    LocalAccountFlows,
    WeakPassword,
)
from cloud_agent.domain.accounts import (
    AccountNotFound,
    AccountTransitionRefused,
    AccountVersionConflict,
    WaitlistNotResubmittable,
)
from cloud_agent.identity import session_cookie_is_secure
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import AccountEntry, WaitlistEntry
from cloud_agent.schemas.session import SessionUserResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from fastapi import BackgroundTasks, Response

    from cloud_agent.accounts.mail import AccountMailer
    from cloud_agent.agent_access import AgentAccessDecision
    from cloud_agent.domain.accounts import AccountRecord
    from cloud_agent.ports.accounts import LocalAccountRepository

logger = get_logger(__name__)

#: One rule: ``(bucket, key, window seconds, attempts allowed in the window)``.
RateRule = tuple[str, str, int, int]

#: The fixed answer for each refusal a flow raises.
_REFUSALS: dict[type[Exception], tuple[int, str, str | None]] = {
    InvalidEmail: (400, "invalid_email", "Enter a valid email address."),
    WeakPassword: (400, "weak_password", None),
    InvalidCredentials: (401, "invalid_credentials", "Invalid email or password."),
    EmailNotVerified: (
        403,
        "email_not_verified",
        "Email not verified. Please check your email for the verification link.",
    ),
    AccountSuspended: (403, "account_suspended", "This account is suspended."),
    InvalidToken: (400, "invalid_token", "This link is invalid or has expired."),
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
    """The application's mailer: a test's substitute, or SMTP from ``AGENT_SMTP_*``."""
    mailer = getattr(request.app.state, "account_mailer", None)
    if mailer is None:
        mailer = SmtpAccountMailer()
        request.app.state.account_mailer = mailer
    return mailer


def local_accounts(request: Request) -> LocalAccountRepository:
    """The local-account repository, or 503 without a database."""
    return require_store(get_storage(request)).local_accounts


def _web_base_or_none() -> str | None:
    try:
        return mail_web_base_url(allow_http=not session_cookie_is_secure())
    except LocalAuthNotConfigured:
        return None


def signup_enabled(request: Request) -> bool:
    """Whether sign-up is offered: switched on, and able to mail its links."""
    return (
        local_mode()
        and signup_flag_enabled()
        and account_mailer(request).configured
        and _web_base_or_none() is not None
    )


def local_flows(
    request: Request, background: BackgroundTasks | None = None, *, mails: bool = False
) -> LocalAccountFlows:
    """The flows for one request; mail is sent after the response.

    A flow that mails checks its configuration before touching the database,
    so a misconfigured deployment refuses every address the same way.
    """
    mailer = account_mailer(request)
    base = _web_base_or_none() if mails else ""
    if base is None or (mails and not mailer.configured):
        raise AgentConfigurationError()

    def send(mail: Any) -> None:
        if background is not None:
            background.add_task(mailer.send, mail)

    return LocalAccountFlows(local_accounts(request), web_base_url=lambda: base or "", send=send)


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
    )


def local_session_user(record: AccountRecord, decision: AgentAccessDecision) -> SessionUserResponse:
    """The session body for a local account: identity, current access and application."""
    return SessionUserResponse(
        id=record.id,
        agent_access=decision.public(),
        auth_mode="local",
        email=record.email,
        waitlist=waitlist_entry(record),
    )


__all__ = [
    "LocalAuthRoute",
    "RateRule",
    "account_entry",
    "account_mailer",
    "client_bucket",
    "enforce_rate_limits",
    "local_accounts",
    "local_flows",
    "local_session_user",
    "refuse_in_local_mode",
    "require_json",
    "require_local_mode",
    "signup_enabled",
    "terminal_open_fence",
    "waitlist_entry",
]
