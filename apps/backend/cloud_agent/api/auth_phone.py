"""Phone sign-up, sign-in and codes: ``/v1/auth/phone/*`` (``AGENT_AUTH_MODE=local``).

Adapted from HybridInference ``phone_auth_routes.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c)
2026 Harvard SEAS). Off unless an administrator turns phone sign-in on and
configures a provider; every route then answers 503 ``phone_unavailable``
before it counts, reserves or sends anything.

Sends are limited per address first and then per number, each window counted
in Postgres, so one address refused at its own limit does not use up a
number's allowance; codes are checked against the same two kinds of window
and, above all, against each code's own attempts (see
:mod:`cloud_agent.application.phone_accounts`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from cloud_agent.accounts.captcha import verify_captcha
from cloud_agent.accounts.config import signup_flag_enabled
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    client_bucket,
    enforce_rate_limits,
    local_accounts,
    local_flows,
    require_json,
    sms_sender,
)
from cloud_agent.api._local_browser import complete_sign_in, joinable_browser
from cloud_agent.api.auth import (
    DAY,
    FIFTEEN_MIN,
    HOUR,
    record_sign_in,
    require_terms,
    signed_in_response,
    signup_rules,
)
from cloud_agent.api.deps import get_client_ip, get_owner_directory, local_session_owner
from cloud_agent.api.errors import typed_error
from cloud_agent.application.accounts import InvalidCredentials
from cloud_agent.application.phone_accounts import (
    ACCOUNT_PURPOSES,
    InvalidPhoneCode,
    PhoneFlows,
    normalize_phone,
)
from cloud_agent.schemas.accounts import (
    PhoneCodeRequest,
    PhoneCodeResponse,
    PhoneSignupRequest,
    PhoneVerifyRequest,
    SignupResponse,
)
from cloud_agent.schemas.session import SessionUserResponse
from cloud_agent.settings import integer_setting

if TYPE_CHECKING:
    from cloud_agent.domain.accounts import AccountRecord

router = APIRouter(
    prefix="/v1/auth/phone",
    tags=["auth"],
    route_class=LocalAuthRoute,
    dependencies=[Depends(require_json)],
)


def phone_flows(request: Request, background: BackgroundTasks | None = None) -> PhoneFlows:
    """The phone flows for one request: a test's provider, or the configured one."""
    return PhoneFlows(local_flows(request, background), sms_sender(request))


async def enforce_code_checks(request: Request, phone: str) -> None:
    """Count one code check, per address and then per number; 429 if over."""
    await enforce_rate_limits(
        request,
        [
            (
                "phone_verify_ip",
                client_bucket(request),
                HOUR,
                integer_setting("accounts.sms_verify_per_ip_hour"),
            )
        ],
    )
    await enforce_rate_limits(
        request,
        [
            (
                "phone_verify_phone",
                phone,
                FIFTEEN_MIN,
                integer_setting("accounts.sms_verify_per_phone_15min"),
            )
        ],
    )


async def _send_quota(request: Request, phone: str) -> None:
    address = client_bucket(request)
    await enforce_rate_limits(
        request,
        [
            ("phone_send_ip", address, HOUR, integer_setting("accounts.sms_send_per_ip_hour")),
            ("phone_send_ip_day", address, DAY, integer_setting("accounts.sms_send_per_ip_day")),
        ],
    )
    await enforce_rate_limits(
        request,
        [
            ("phone_send", phone, HOUR, integer_setting("accounts.sms_send_per_phone_hour")),
            ("phone_send_day", phone, DAY, integer_setting("accounts.sms_send_per_phone_day")),
        ],
    )


async def _code_account(request: Request, purpose: str) -> AccountRecord | None:
    if purpose not in ACCOUNT_PURPOSES:
        return None
    owner = await local_session_owner(request, request.headers.get("authorization"))
    if owner is None:
        raise HTTPException(status_code=401, detail="Missing authentication token.")
    return owner.account


@router.post("/code", response_model=PhoneCodeResponse)
async def send_phone_code(body: PhoneCodeRequest, request: Request) -> PhoneCodeResponse:
    """Send a purpose-bound code where it can be used; the same answer for any number."""
    flows = phone_flows(request)
    flows.require_enabled()
    if body.purpose == "signup" and not signup_flag_enabled():
        raise typed_error(403, "signup_disabled", "Sign-up is not open on this deployment.")
    account = await _code_account(request, body.purpose)
    phone = normalize_phone(body.phone)
    if not await verify_captcha(body.turnstile_token, get_client_ip(request)):
        raise typed_error(400, "captcha_failed", "Captcha verification failed.")
    await _send_quota(request, phone)
    await flows.send_code(phone, body.purpose, account.id if account else None)
    return PhoneCodeResponse(retry_after=integer_setting("accounts.sms_send_cooldown_s"))


@router.post("/signup", response_model=SignupResponse, status_code=201)
async def phone_sign_up(
    body: PhoneSignupRequest, request: Request, background: BackgroundTasks
) -> SignupResponse:
    """Create a verified phone account from a sign-up code."""
    flows = phone_flows(request, background)
    flows.require_enabled()
    if not signup_flag_enabled():
        raise typed_error(403, "signup_disabled", "Sign-up is not open on this deployment.")
    await enforce_rate_limits(request, signup_rules(request))
    require_terms(body.accepted_tos)
    phone = normalize_phone(body.phone)
    await enforce_code_checks(request, phone)
    outcome = await flows.sign_up(
        phone, body.code, user_name=body.user_name, use_case=body.use_case
    )
    if outcome.account is not None:
        await local_accounts(request).record_account_audit(
            action="agent.local_account.signup",
            actor_id="anonymous",
            admin_ip=get_client_ip(request),
            account_id=outcome.account.id,
            details={"method": "phone", "applied": True},
        )
    return SignupResponse(requires_approval=outcome.waits_for_approval)


@router.post("/login", response_model=SessionUserResponse, response_model_exclude_none=True)
async def phone_sign_in(
    body: PhoneVerifyRequest,
    request: Request,
    response: Response,
    owners: Any = Depends(get_owner_directory),
) -> SessionUserResponse:
    """Spend a login code and add the account to this browser."""
    flows = phone_flows(request)
    flows.require_enabled()
    phone = normalize_phone(body.phone)
    key = f"phone:{phone}"
    try:
        await enforce_code_checks(request, phone)
        signed_in = await flows.sign_in(phone, body.code)
        browser = await joinable_browser(request, signed_in)
        await complete_sign_in(request, response, browser, signed_in)
    except (InvalidPhoneCode, InvalidCredentials) as exc:
        reason = "invalid_code" if isinstance(exc, InvalidPhoneCode) else "user_not_found"
        await record_sign_in(
            request, method="phone", identifier=key, account_id=None, failure=reason
        )
        raise
    except HTTPException as exc:
        if exc.status_code == 429:
            await record_sign_in(
                request, method="phone", identifier=key, account_id=None, failure="rate_limited"
            )
        raise
    await record_sign_in(
        request, method="phone", identifier=key, account_id=signed_in.account.id, failure=None
    )
    return await signed_in_response(request, response, owners, signed_in)


__all__ = ["enforce_code_checks", "phone_flows", "router"]
