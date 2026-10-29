"""Local sign-up and sign-in: ``/v1/auth/*`` (``AGENT_AUTH_MODE=local``).

``GET /v1/auth/config`` answers in both modes; every other route answers 404
``local_auth_disabled`` in Gateway mode, where sign-in stays the Gateway's.

Each unsafe route here is checked by the cross-site write guard even without a
session cookie (sign-in CSRF would otherwise put the attacker's account in the
victim's browser), and accepts only a JSON body. Verification and reset are
explicit POSTs from the web page that read the token from the link's fragment;
following a link changes nothing by itself.

Abuse limits are counted in Postgres, per address and per email, so every
worker and replica shares them. The windows follow HybridInference's defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from cloud_agent.accounts.config import auth_mode
from cloud_agent.accounts.session_cookie import presented_local_token, set_local_session_cookie
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    client_bucket,
    enforce_rate_limits,
    local_flows,
    local_session_user,
    require_json,
    require_local_mode,
    signup_enabled,
)
from cloud_agent.api.deps import bearer_token, get_gateway_client, resolve_agent_access
from cloud_agent.api.errors import typed_error
from cloud_agent.application.accounts import InvalidEmail, normalize_email
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import (
    AuthConfigResponse,
    EmailPasswordRequest,
    EmailRequest,
    OkResponse,
    ResetPasswordRequest,
    TokenRequest,
)
from cloud_agent.schemas.session import SessionUserResponse

if TYPE_CHECKING:
    from cloud_agent.api._local_auth import RateRule

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"], route_class=LocalAuthRoute)

_FIFTEEN_MIN = 15 * 60
_HOUR = 60 * 60
_DAY = 24 * _HOUR

_LOCAL_WRITE = [Depends(require_local_mode), Depends(require_json)]


def _email_key(email: str) -> str:
    # Counted by the address as typed, case-folded: an attacker gains nothing
    # by spelling it differently, and no lookup decides the key.
    return email.strip().lower()


def _login_key(email: str) -> str:
    # Counted by the account the address would sign in to, so every spelling
    # of one mailbox shares its windows. An address that cannot sign in to
    # any account is counted as typed, apart from every real one.
    try:
        _display, normalized = normalize_email(email)
    except InvalidEmail:
        return f"raw:{_email_key(email)}"
    return normalized


async def enforce_login_limits(request: Request, email: str) -> None:
    """Count one sign-in attempt; 429 if it is over.

    Both per-address windows count every attempt, refused or not, and only
    an attempt they both let through counts in the account's own window. One
    address therefore adds at most its own allowance to the account's, so
    whoever guesses from it is refused there without locking the owner out
    everywhere else; filling the account's window takes ten addresses.

    Each window's count is one atomic statement, but the two stages are not
    one transaction (see ``docs/developer/local-accounts.md``).
    """
    key = _login_key(email)
    address = client_bucket(request)
    await enforce_rate_limits(
        request,
        [
            ("login_email_ip", f"{key}\x00{address}", _FIFTEEN_MIN, 5),
            ("login_ip", address, _HOUR, 20),
        ],
    )
    await enforce_rate_limits(request, [("login_email", key, _FIFTEEN_MIN, 50)])


def _mail_rules(request: Request, email: str) -> list[RateRule]:
    return [
        ("mail_ip", client_bucket(request), _HOUR, 10),
        ("mail_email", _email_key(email), _HOUR, 3),
        ("mail_email_day", _email_key(email), _DAY, 10),
    ]


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config(request: Request) -> AuthConfigResponse:
    """Which sign-in this deployment offers."""
    mode = auth_mode()
    return AuthConfigResponse(mode=mode, signup_enabled=mode == "local" and signup_enabled(request))


@router.post("/signup", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def sign_up(
    body: EmailPasswordRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Create an account and mail its verification link; the same answer for a taken address."""
    if not signup_enabled(request):
        raise typed_error(403, "signup_disabled", "Sign-up is not open on this deployment.")
    await enforce_rate_limits(
        request,
        [
            ("signup_ip", client_bucket(request), _HOUR, 5),
            ("signup_ip_day", client_bucket(request), _DAY, 10),
            *_mail_rules(request, body.email),
        ],
    )
    await local_flows(request, background, mails=True).sign_up(body.email, body.password)
    return OkResponse()


@router.post("/login", response_model=SessionUserResponse, response_model_exclude_none=True)
async def sign_in(
    body: EmailPasswordRequest,
    request: Request,
    response: Response,
    _local: None = Depends(require_local_mode),
    _json: None = Depends(require_json),
    gateway: Any = Depends(get_gateway_client),
) -> SessionUserResponse:
    """Check the password and set the session cookie."""
    await enforce_login_limits(request, body.email)
    flows = local_flows(request)
    signed_in = await flows.sign_in(body.email, body.password)
    # A session this browser already held is ended rather than left behind.
    await flows.sign_out(
        presented_local_token(request.cookies, bearer_token(request.headers.get("authorization")))
    )
    decision = await resolve_agent_access(gateway, signed_in.account.id)
    set_local_session_cookie(response, signed_in.token)
    logger.info(
        "agent_local_session_established",
        extra={"event": "agent_local_session_established", "user_id": signed_in.account.id},
    )
    return local_session_user(signed_in.account, decision)


@router.post("/verify-email", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def verify_email(body: TokenRequest, request: Request) -> OkResponse:
    """Redeem a verification link. Does not sign in."""
    await enforce_rate_limits(request, [("token_ip", client_bucket(request), _FIFTEEN_MIN, 30)])
    await local_flows(request).verify_email(body.token)
    return OkResponse()


@router.post("/resend-verification", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def resend_verification(
    body: EmailRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Mail a fresh verification link, if the address has an unverified account."""
    await enforce_rate_limits(request, _mail_rules(request, body.email))
    await local_flows(request, background, mails=True).resend_verification(body.email)
    return OkResponse()


@router.post("/forgot-password", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def forgot_password(
    body: EmailRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Mail a reset link, if the address has an account."""
    await enforce_rate_limits(request, _mail_rules(request, body.email))
    await local_flows(request, background, mails=True).forgot_password(body.email)
    return OkResponse()


@router.post("/reset-password", response_model=OkResponse, dependencies=_LOCAL_WRITE)
async def reset_password(body: ResetPasswordRequest, request: Request) -> OkResponse:
    """Set a new password from a reset link; every session of the account ends."""
    await enforce_rate_limits(request, [("token_ip", client_bucket(request), _FIFTEEN_MIN, 30)])
    await local_flows(request).reset_password(body.token, body.password)
    return OkResponse()


__all__ = ["router"]
