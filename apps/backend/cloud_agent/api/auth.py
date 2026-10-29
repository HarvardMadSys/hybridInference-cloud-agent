"""Local sign-up and sign-in: ``/v1/auth/*`` (``AGENT_AUTH_MODE=local``).

``GET /v1/auth/config`` answers in both modes; every other route answers 404
``local_auth_disabled`` in Gateway mode, where sign-in stays the Gateway's.

Each unsafe route here is checked by the cross-site write guard even without a
session cookie (sign-in CSRF would otherwise put the attacker's account in the
victim's browser), and accepts only a JSON body. Verification and reset are
explicit POSTs from the web page that read the token from the link's fragment;
following a link changes nothing by itself.

Abuse limits are counted in Postgres, per address and per identifier, so every
worker and replica shares them. The windows follow HybridInference's defaults.
Phone sign-in, the browser's accounts and the signed-in owner's own changes
are in :mod:`~cloud_agent.api.auth_phone`, :mod:`~cloud_agent.api.auth_browser`
and :mod:`~cloud_agent.api.auth_account`.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from cloud_agent.accounts import tokens
from cloud_agent.accounts.captcha import site_key, verify_captcha
from cloud_agent.accounts.config import auth_mode, local_session_ttl_s, signup_flag_enabled
from cloud_agent.accounts.device_cookie import recognized_device
from cloud_agent.accounts.passwords import hash_password
from cloud_agent.accounts.policy import require_email_verification, terms_url
from cloud_agent.accounts.setup import new_setup_code, normalize_setup_code
from cloud_agent.accounts.sms import sms_enabled
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    client_bucket,
    enforce_rate_limits,
    local_accounts,
    local_flows,
    local_session_user,
    mail_available,
    require_json,
    require_local_mode,
    signup_enabled,
    sms_sender,
)
from cloud_agent.api._local_browser import (
    MAX_ACCOUNTS_PER_BROWSER,
    complete_sign_in,
    device_identifiers,
    joinable_browser,
    prepare_sign_in,
)
from cloud_agent.api.deps import get_client_ip, get_gateway_client, resolve_agent_access
from cloud_agent.api.errors import typed_error
from cloud_agent.application.accounts import (
    AccountDeleted,
    AccountSuspended,
    EmailNotVerified,
    InvalidCredentials,
    SignedIn,
    identifier_key,
    normalize_login_name,
    require_strong_password,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.accounts import BrowserFull
from cloud_agent.schemas.accounts import (
    AuthConfigResponse,
    AuthOptionsResponse,
    EmailRequest,
    LoginRequest,
    OkResponse,
    PhoneAuthConfig,
    ResetPasswordRequest,
    SetupAdminRequest,
    SignupRequest,
    TokenRequest,
)
from cloud_agent.schemas.session import SessionUserResponse
from cloud_agent.settings import boolean_setting, integer_setting, text_setting

if TYPE_CHECKING:
    from cloud_agent.api._local_auth import RateRule

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"], route_class=LocalAuthRoute)

FIFTEEN_MIN = 15 * 60
HOUR = 60 * 60
DAY = 24 * HOUR

_LOCAL_WRITE = [Depends(require_local_mode), Depends(require_json)]

#: Why a sign-in failed, as the event log records it.
_FAILURE_REASONS: dict[type[Exception], str] = {
    AccountDeleted: "account_deleted",
    AccountSuspended: "account_inactive",
    EmailNotVerified: "email_unverified",
    BrowserFull: "browser_account_limit",
}


async def enforce_login_limits(request: Request, identifier: str) -> None:
    """Count one password attempt for an identifier as typed; 429 if it is over.

    Counted by the key of the account it would sign in to
    (:func:`~cloud_agent.application.accounts.identifier_key`), so every
    spelling of one mailbox or name shares its windows.

    A browser presenting a device-cookie entry for the identifier, from an
    earlier sign-in, is counted in its own window alone, so nobody else's
    attempts, from any address, lock it out. Otherwise both per-address
    windows count every attempt, refused or not, and only an attempt they both
    let through counts in the account's own window: one address adds at most
    its own allowance to the account's, so whoever guesses from it is refused
    there without locking the owner out everywhere else.

    Each window's count is one atomic statement, but the two stages are not
    one transaction (see ``docs/developer/local-accounts.md``).
    """
    key = identifier_key(identifier)
    per_address = integer_setting("accounts.login_per_15min")
    device = recognized_device(request.cookies, key)
    if device is not None:
        await enforce_rate_limits(request, [("login_device", device, FIFTEEN_MIN, per_address)])
        return
    address = client_bucket(request)
    await enforce_rate_limits(
        request,
        [
            ("login_email_ip", f"{key}\x00{address}", FIFTEEN_MIN, per_address),
            ("login_ip", address, HOUR, integer_setting("accounts.login_ip_per_hour")),
        ],
    )
    await enforce_rate_limits(
        request,
        [("login_email", key, FIFTEEN_MIN, integer_setting("accounts.login_account_per_15min"))],
    )


def mail_rules(request: Request, email: str) -> list[RateRule]:
    """The windows a request that mails an address counts in."""
    key = identifier_key(email)
    return [
        ("mail_ip", client_bucket(request), HOUR, 10),
        ("mail_email", key, HOUR, 3),
        ("mail_email_day", key, DAY, 10),
    ]


async def record_sign_in(
    request: Request,
    *,
    method: str,
    identifier: str,
    account_id: str | None,
    failure: str | None,
) -> None:
    """Best-effort sign-in event; a failure to record never breaks the sign-in."""
    try:
        await local_accounts(request).record_login_event(
            account_id=account_id,
            method=method,
            identifier=identifier,
            outcome="failure" if failure else "success",
            failure_reason=failure,
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.warning(
            "agent_login_event_unrecorded outcome=%s",
            "failure" if failure else "success",
            extra={"event": "agent_login_event_unrecorded"},
        )


async def signed_in_response(
    request: Request, response: Response, gateway: Any, signed_in: SignedIn
) -> SessionUserResponse:
    """The session body once a sign-in has joined the browser."""
    decision = await resolve_agent_access(gateway, signed_in.account.id)
    logger.info(
        "agent_local_session_established",
        extra={"event": "agent_local_session_established", "user_id": signed_in.account.id},
    )
    return local_session_user(signed_in.account, decision)


async def _setup_pending(request: Request) -> bool:
    try:
        return await local_accounts(request).setup_pending()
    except Exception:
        return False


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config(request: Request) -> AuthConfigResponse:
    """Which sign-in this deployment offers: read before every page renders, so kept small."""
    mode = auth_mode()
    return AuthConfigResponse(mode=mode, signup_enabled=mode == "local" and signup_enabled(request))


@router.get(
    "/options", response_model=AuthOptionsResponse, dependencies=[Depends(require_local_mode)]
)
async def auth_options(request: Request) -> AuthOptionsResponse:
    """What the sign-in, sign-up and account pages offer. Public; carries no secret."""
    phone_on = sms_enabled(sms_sender(request))
    prefixes = [
        item.strip()
        for item in text_setting("accounts.sms_allowed_prefixes").split(",")
        if item.strip()
    ]
    return AuthOptionsResponse(
        signup_enabled=signup_enabled(request),
        phone_signup_enabled=phone_on and signup_flag_enabled(),
        email_delivery=mail_available(request),
        require_email_verification=require_email_verification(),
        phone=PhoneAuthConfig(
            enabled=phone_on,
            default_country_code=(
                text_setting("accounts.sms_default_country_code") if phone_on else ""
            ),
            allowed_prefixes=prefixes if phone_on else [],
        ),
        turnstile_site_key=site_key(),
        terms_url=terms_url(),
        terms_text=text_setting("accounts.terms_text").strip(),
        setup_required=await _setup_pending(request),
        max_accounts_per_browser=MAX_ACCOUNTS_PER_BROWSER,
    )


def require_terms(accepted_tos: bool) -> None:
    """Refuse a sign-up without the Terms of Service agreed to."""
    if not accepted_tos:
        raise typed_error(
            400, "tos_required", "You must agree to the Terms of Service to create an account."
        )


async def require_signup_consent(
    request: Request, accepted_tos: bool, turnstile_token: str | None
) -> None:
    """Refuse a sign-up without the Terms agreed to, or whose captcha does not verify."""
    require_terms(accepted_tos)
    if not await verify_captcha(turnstile_token, get_client_ip(request)):
        raise typed_error(400, "captcha_failed", "Captcha verification failed.")


def signup_rules(request: Request) -> list[RateRule]:
    """The sign-up windows: per address, or one shared budget when there is no address.

    As HybridInference's ``check_and_record_signup``: a request whose client
    address cannot be told (``unknown``, behind a proxy this deployment does
    not trust) shares one coarse budget with every other such request, rather
    than pretending the proxy is one client; ``accounts.signup_require_resolved_ip``
    refuses it outright instead (503).
    """
    address = client_bucket(request)
    if address == "unknown":
        if boolean_setting("accounts.signup_require_resolved_ip"):
            raise typed_error(
                503,
                "client_unresolved",
                "Sign-up is temporarily unavailable because the client address "
                "could not be verified.",
            )
        return [
            ("signup_unresolved", "", HOUR, integer_setting("accounts.unresolved_signup_per_hour")),
            (
                "signup_unresolved_day",
                "",
                DAY,
                integer_setting("accounts.unresolved_signup_per_day"),
            ),
        ]
    return [
        ("signup_ip", address, HOUR, integer_setting("accounts.signup_per_hour")),
        ("signup_ip_day", address, DAY, integer_setting("accounts.signup_per_day")),
    ]


@router.post("/signup", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def sign_up(body: SignupRequest, request: Request, background: BackgroundTasks) -> OkResponse:
    """Create an account and mail its verification link; the same answer for a taken address.

    The answer is identical whether or not the address had an account, so it
    tells nothing about who exists; the page explains what happens next from
    the deployment's options instead.
    """
    if not signup_enabled(request):
        raise typed_error(403, "signup_disabled", "Sign-up is not open on this deployment.")
    await enforce_rate_limits(request, [*signup_rules(request), *mail_rules(request, body.email)])
    await require_signup_consent(request, body.accepted_tos, body.turnstile_token)
    flows = local_flows(request, background, mails=require_email_verification())
    outcome = await flows.sign_up(
        body.email,
        body.password,
        user_name=body.user_name,
        use_case=body.use_case,
        tos_accepted=True,
    )
    if outcome.account is not None:
        await local_accounts(request).record_account_audit(
            action="agent.local_account.signup",
            actor_id="anonymous",
            admin_ip=get_client_ip(request),
            account_id=outcome.account.id,
            details={"method": "email", "applied": body.use_case is not None},
        )
    return OkResponse()


@router.post("/login", response_model=SessionUserResponse, response_model_exclude_none=True)
async def sign_in(
    body: LoginRequest,
    request: Request,
    response: Response,
    _local: None = Depends(require_local_mode),
    _json: None = Depends(require_json),
    gateway: Any = Depends(get_gateway_client),
) -> SessionUserResponse:
    """Check the password, add the account to this browser and set the cookie.

    The identifier is an email address or, for an account created without
    one, its login name.
    """
    key = identifier_key(body.name)
    try:
        await enforce_login_limits(request, body.name)
    except HTTPException as exc:
        if exc.status_code == 429:
            await record_sign_in(
                request, method="password", identifier=key, account_id=None, failure="rate_limited"
            )
        raise
    flows = local_flows(request)
    try:
        signed_in = await flows.sign_in(body.name, body.password)
        browser = await joinable_browser(request, signed_in)
        await complete_sign_in(
            request,
            response,
            browser,
            signed_in,
            password_identifiers=device_identifiers(signed_in.account),
        )
    except InvalidCredentials as exc:
        failure = "invalid_password" if exc.account_id else "user_not_found"
        await record_sign_in(
            request, method="password", identifier=key, account_id=exc.account_id, failure=failure
        )
        raise
    except tuple(_FAILURE_REASONS) as exc:
        await record_sign_in(
            request,
            method="password",
            identifier=key,
            account_id=getattr(exc, "account_id", None),
            failure=_FAILURE_REASONS[type(exc)],
        )
        raise
    await record_sign_in(
        request, method="password", identifier=key, account_id=signed_in.account.id, failure=None
    )
    return await signed_in_response(request, response, gateway, signed_in)


@router.post("/verify-email", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def verify_email(
    body: TokenRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Redeem a verification or address-binding link. Does not sign in."""
    await enforce_rate_limits(request, [("token_ip", client_bucket(request), FIFTEEN_MIN, 30)])
    await local_flows(request, background).redeem_email_link(body.token)
    return OkResponse()


@router.post("/resend-verification", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def resend_verification(
    body: EmailRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Mail a fresh verification link, if the address has an unverified account."""
    await enforce_rate_limits(request, mail_rules(request, body.email))
    await local_flows(request, background, mails=True).resend_verification(body.email)
    return OkResponse()


@router.post("/forgot-password", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def forgot_password(
    body: EmailRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Mail a reset link, if the address has an account."""
    await enforce_rate_limits(request, mail_rules(request, body.email))
    await local_flows(request, background, mails=True).forgot_password(body.email)
    return OkResponse()


@router.post("/reset-password", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def reset_password(body: ResetPasswordRequest, request: Request) -> OkResponse:
    """Set a new password from a reset link; every session of the account ends."""
    await enforce_rate_limits(request, [("token_ip", client_bucket(request), FIFTEEN_MIN, 30)])
    await local_flows(request).reset_password(body.token, body.password)
    return OkResponse()


# ── First-run setup ──────────────────────────────────────────────────────


@router.post(
    "/setup/admin",
    response_model=SessionUserResponse,
    response_model_exclude_none=True,
    dependencies=_LOCAL_WRITE,
)
async def create_setup_admin(
    body: SetupAdminRequest,
    request: Request,
    response: Response,
    gateway: Any = Depends(get_gateway_client),
) -> SessionUserResponse:
    """Create the deployment's first administrator with the setup code, and sign them in.

    The administrator signs in with a login name, needs no email and no SMTP,
    and is verified, approved and an administrator at once. Only a wrong code
    counts against its own per-address limit (ten in 15 minutes), so junk sent
    from an address the operator shares cannot lock the operator out; the
    code's 60 random bits are what make guessing it hopeless.
    """
    accounts = local_accounts(request)
    login_name = normalize_login_name(body.login_name)
    require_strong_password(body.password)
    pending = await accounts.setup_code(new_setup_code())
    if pending is None:
        raise typed_error(409, "setup_complete", "This deployment has already been set up.")
    presented = normalize_setup_code(body.setup_code)
    if not secrets.compare_digest(presented.encode(), pending.encode()):
        await enforce_rate_limits(
            request, [("setup_code_ip", client_bucket(request), FIFTEEN_MIN, 10)]
        )
        raise typed_error(403, "invalid_setup_code", "That setup code is not valid.")
    # A full browser refuses before the administrator exists, not after.
    browser = await prepare_sign_in(request, None)
    account_id = tokens.new_account_id()
    password_hash = await hash_password(body.password)
    created = await accounts.complete_setup(
        account_id=account_id,
        login_name=login_name,
        password_hash=password_hash,
        user_name=body.display_name,
        admin_ip=get_client_ip(request),
    )
    if created is None:
        raise typed_error(409, "setup_complete", "This deployment has already been set up.")
    token = tokens.new_session_token()
    session_id = tokens.new_session_id()
    account = await accounts.create_session(
        session_id=session_id,
        account_id=account_id,
        token_hash=tokens.session_hash(token),
        ttl_s=local_session_ttl_s(),
        password_hash=password_hash,
    )
    assert account is not None
    signed_in = SignedIn(token=token, account=account, session_id=session_id)
    await complete_sign_in(
        request,
        response,
        browser,
        signed_in,
        password_identifiers=device_identifiers(account),
    )
    await record_sign_in(
        request,
        method="setup",
        identifier=identifier_key(login_name),
        account_id=account_id,
        failure=None,
    )
    return await signed_in_response(request, response, gateway, signed_in)


__all__ = [
    "DAY",
    "FIFTEEN_MIN",
    "HOUR",
    "enforce_login_limits",
    "mail_rules",
    "record_sign_in",
    "require_signup_consent",
    "require_terms",
    "router",
    "signed_in_response",
    "signup_rules",
]
