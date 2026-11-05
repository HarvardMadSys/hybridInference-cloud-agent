"""The accounts signed in on this browser: ``/v1/auth/accounts``, switching, signing out.

Adapted from HybridInference ``auth_routes.py`` (``/auth/accounts``,
``/auth/switch``, ``DELETE /auth/accounts/{id}``, ``/auth/logout-all``) at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c)
2026 Harvard SEAS). Authenticated by the browser's cookie alone: the chooser is
shown exactly when no account is active. Nothing here returns a credential.

Switching needs no password: the account signed in here earlier and its own
session is still live. A tab still showing the previous account is refused
(409 ``account_switched``) on its next request, and its open streams end at
their next re-check, so nothing it does lands on the new account.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from cloud_agent.accounts.session_cookie import expire_local_session_cookies
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    local_accounts,
    local_session_user,
    require_json,
)
from cloud_agent.api._local_browser import (
    MAX_ACCOUNTS_PER_BROWSER,
    current_browser,
    sign_out_of_browser,
)
from cloud_agent.api.deps import get_owner_directory, resolve_agent_access
from cloud_agent.api.errors import typed_error
from cloud_agent.domain.accounts import may_sign_in, permissions_for
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import (
    BrowserAccountEntry,
    BrowserAccountsResponse,
    OkResponse,
    SwitchAccountRequest,
)
from cloud_agent.schemas.session import SessionUserResponse

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/auth",
    tags=["auth"],
    route_class=LocalAuthRoute,
)


def _summaries(browser: Any) -> BrowserAccountsResponse:
    if browser is None:
        return BrowserAccountsResponse(
            active_account_id=None, accounts=[], max_accounts=MAX_ACCOUNTS_PER_BROWSER
        )
    entries = []
    for member in browser.accounts:
        signed_in = member.session_live and may_sign_in(member.account)
        entries.append(
            BrowserAccountEntry(
                id=member.account.id,
                label=member.account.label,
                user_name=member.account.user_name,
                active=member.account.id == browser.active_account_id,
                signed_in=signed_in,
                agent_access=("agent.use" in permissions_for(member.account))
                if signed_in
                else None,
            )
        )
    return BrowserAccountsResponse(
        active_account_id=browser.active_account_id,
        accounts=entries,
        max_accounts=MAX_ACCOUNTS_PER_BROWSER,
    )


@router.get("/accounts", response_model=BrowserAccountsResponse)
async def list_browser_accounts(request: Request) -> BrowserAccountsResponse:
    """The accounts signed in on this browser, oldest first, for the chooser."""
    return _summaries(await current_browser(request))


@router.post(
    "/switch",
    response_model=SessionUserResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_json)],
)
async def switch_account(
    body: SwitchAccountRequest, request: Request, owners: Any = Depends(get_owner_directory)
) -> SessionUserResponse:
    """Make another account signed in on this browser the active one."""
    browser = await current_browser(request)
    member = browser.member(body.account_id) if browser is not None else None
    if browser is None or member is None:
        raise typed_error(401, "not_signed_in", "That account is not signed in on this browser.")
    if not member.session_live or not may_sign_in(member.account):
        raise typed_error(
            401, "session_ended", "This account's session has ended. Please sign in again."
        )
    if not await local_accounts(request).set_browser_active(
        browser_id=browser.id, account_id=member.account.id
    ):
        raise typed_error(401, "not_signed_in", "That account is not signed in on this browser.")
    logger.info(
        "agent_local_browser_switched",
        extra={"event": "agent_local_browser_switched", "user_id": member.account.id},
    )
    decision = await resolve_agent_access(owners, member.account.id)
    return local_session_user(member.account, decision)


@router.delete("/accounts/{account_id}", response_model=BrowserAccountsResponse)
async def remove_browser_account(
    account_id: str, request: Request, response: Response
) -> BrowserAccountsResponse:
    """Sign one account out of this browser and drop it from the chooser.

    Works for an account whose session already ended, so the chooser can
    forget it; the cookie goes with the last account.
    """
    browser = await current_browser(request)
    if browser is None or browser.member(account_id) is None:
        raise typed_error(404, "not_found", "That account is not signed in here.")
    await sign_out_of_browser(request, response, browser, account_id)
    request.state.local_browser = None
    return _summaries(await current_browser(request))


@router.post("/logout-all", response_model=OkResponse, dependencies=[Depends(require_json)])
async def sign_out_everywhere_here(request: Request, response: Response) -> OkResponse:
    """Sign every account out of this browser and drop its cookie."""
    browser = await current_browser(request)
    if browser is not None:
        await local_accounts(request).revoke_browser(browser_id=browser.id)
    expire_local_session_cookies(response)
    return OkResponse()


__all__ = ["router"]
