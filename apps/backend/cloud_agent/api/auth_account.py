"""The signed-in owner's own account: ``/v1/auth/account/*`` (``AGENT_AUTH_MODE=local``).

Changing or setting the password, binding or changing the email, binding a
phone number and deleting the account, each as HybridInference allows (see
:mod:`cloud_agent.application.account_security`). Identity only: an account
without Agent use manages itself too. Under ``/v1/auth`` so the cross-site
write guard checks every request and the device cookie is sent with it.

Every proof of the owner counts against the same limits as signing in: a
password against the sign-in windows (with the device cookie's own window),
a phone code against the code windows. Deleting an account then stops its
work through the durable termination record its transaction opened, in the
background and again from the enforcement pass until acknowledged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from cloud_agent.accounts.session_cookie import expire_local_session_cookies
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    account_identity,
    local_accounts,
    mail_available,
    require_json,
)
from cloud_agent.api._local_browser import current_browser
from cloud_agent.api.auth import enforce_login_limits
from cloud_agent.api.auth_phone import enforce_code_checks, phone_flows
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    local_session_owner,
    require_store,
)
from cloud_agent.api.errors import typed_error
from cloud_agent.application import account_security
from cloud_agent.application.owner_termination import terminate_owner_access
from cloud_agent.application.phone_accounts import normalize_phone
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import (
    AccountIdentity,
    BindEmailRequest,
    BindEmailResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
    LocaleRequest,
    LocaleResponse,
    OkResponse,
    PhoneVerifyRequest,
)

if TYPE_CHECKING:
    from cloud_agent.ports.accounts import SessionOwner

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/auth/account",
    tags=["auth"],
    route_class=LocalAuthRoute,
)


async def _owner(request: Request) -> SessionOwner:
    owner = await local_session_owner(request, request.headers.get("authorization"))
    if owner is None:
        raise HTTPException(status_code=401, detail="Missing authentication token.")
    return owner


async def _count_proof(request: Request, owner: SessionOwner) -> None:
    """Count the proof the request gives against the limits signing in uses."""
    account = owner.account
    if account.has_password:
        await enforce_login_limits(request, account.email or account.login_name or account.id)
    elif account.phone:
        await enforce_code_checks(request, account.phone)


@router.get("", response_model=AccountIdentity)
async def read_account(request: Request) -> AccountIdentity:
    """How the signed-in account signs in."""
    return account_identity((await _owner(request)).account)


@router.put("/locale", response_model=LocaleResponse, dependencies=[Depends(require_json)])
async def save_locale(body: LocaleRequest, request: Request) -> LocaleResponse:
    """Keep the interface language with the account; the most recent choice wins.

    As HybridInference's ``PUT /user/preferences/locale``: a choice carried in
    from a signed-out browser keeps the time it was made, never later than
    now, and one older than the account's stored choice is not written; the
    answer is the language the account keeps, which the page then shows.
    """
    owner = await _owner(request)
    now = datetime.now(UTC)
    chosen = now if body.chosen_at is None else min(body.chosen_at.astimezone(UTC), now)
    locale, chosen_at = await local_accounts(request).set_locale(
        account_id=owner.account.id, locale=body.locale, chosen_at=chosen
    )
    return LocaleResponse(locale=locale, chosen_at=chosen_at)


@router.post("/password", response_model=OkResponse, dependencies=[Depends(require_json)])
async def change_password(
    body: ChangePasswordRequest, request: Request, background: BackgroundTasks
) -> OkResponse:
    """Change the password, or set a first one; every other session is signed out."""
    owner = await _owner(request)
    await _count_proof(request, owner)
    flows = phone_flows(request, background)
    first = await account_security.change_password(
        flows.account_flows,
        flows,
        owner.account,
        session_id=owner.session_id,
        old_password=body.old_password,
        phone_code=body.phone_code,
        new_password=body.new_password,
    )
    await local_accounts(request).record_account_audit(
        action="agent.local_account.password_set"
        if first
        else "agent.local_account.password_change",
        actor_id=f"self:{owner.account.id}",
        admin_ip=get_client_ip(request),
        account_id=owner.account.id,
        details={},
    )
    return OkResponse()


@router.post("/email", response_model=BindEmailResponse, dependencies=[Depends(require_json)])
async def bind_email(
    body: BindEmailRequest, request: Request, background: BackgroundTasks
) -> BindEmailResponse:
    """Mail a link that makes the address the account's email once it is followed.

    The new address is always proven by that link, whatever the sign-up
    verification setting: an address nobody proved is never recorded as
    verified. The current address stays until then, and is told.
    """
    owner = await _owner(request)
    if owner.account.status != "active":
        raise typed_error(403, "account_inactive", "This account is not active.")
    if not mail_available(request):
        raise typed_error(503, "email_unavailable", "Email verification is currently unavailable.")
    await _count_proof(request, owner)
    flows = phone_flows(request, background)
    result = await account_security.request_email_bind(
        flows.account_flows,
        flows,
        owner.account,
        email=body.email,
        password=body.password,
        phone_code=body.phone_code,
    )
    if not result.already_bound:
        await local_accounts(request).record_account_audit(
            action="agent.local_account.request_bind_email",
            actor_id=f"self:{owner.account.id}",
            admin_ip=get_client_ip(request),
            account_id=owner.account.id,
            details={},
        )
    return BindEmailResponse(email=result.email, email_verified=result.already_bound)


@router.post("/phone", response_model=AccountIdentity, dependencies=[Depends(require_json)])
async def bind_phone(body: PhoneVerifyRequest, request: Request) -> AccountIdentity:
    """Bind a phone number to an account that has none, with a ``bind`` code."""
    owner = await _owner(request)
    flows = phone_flows(request)
    flows.require_enabled()
    phone = normalize_phone(body.phone)
    await enforce_code_checks(request, phone)
    await flows.bind(owner.account.id, phone, body.code)
    await local_accounts(request).record_account_audit(
        action="agent.local_account.bind_phone",
        actor_id=f"self:{owner.account.id}",
        admin_ip=get_client_ip(request),
        account_id=owner.account.id,
        details={},
    )
    updated = await local_accounts(request).get_account(owner.account.id)
    assert updated is not None
    return account_identity(updated)


@router.post("/delete", response_model=OkResponse, dependencies=[Depends(require_json)])
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
) -> OkResponse:
    """Delete the signed-in account at its owner's request.

    The same soft delete an administrator performs: the account signs in
    nowhere, it is taken off every browser, its links stop working and its
    work stops, while its data is kept and an administrator can resume it.
    """
    owner = await _owner(request)
    await _count_proof(request, owner)
    flows = phone_flows(request, background)
    deleted = await account_security.delete_own_account(
        flows.account_flows,
        flows,
        owner.account,
        confirm=body.confirm,
        password=body.password,
        phone_code=body.phone_code,
        admin_ip=get_client_ip(request),
    )
    background.add_task(
        _terminate,
        require_store(get_storage(request)),
        deleted.id,
        deleted.version,
    )
    request.state.local_browser = None
    browser = await current_browser(request)
    if browser is None or not browser.accounts:
        expire_local_session_cookies(response)
    logger.info(
        "agent_local_account_self_deleted",
        extra={"event": "agent_local_account_self_deleted", "user_id": deleted.id},
    )
    return OkResponse()


async def _terminate(storage: Any, account_id: str, generation: int) -> None:
    try:
        await terminate_owner_access(storage, account_id, generation=generation)
    except Exception:
        logger.warning(
            "agent_owner_access_termination_deferred",
            exc_info=True,
            extra={"event": "agent_owner_access_termination_deferred", "user_id": account_id},
        )


__all__ = ["router"]
