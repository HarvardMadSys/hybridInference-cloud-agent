"""Signing accounts in and out of a browser (``AGENT_AUTH_MODE=local``).

Adapted from HybridInference ``auth/browser_sessions.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS). One cookie per browser names a browser session holding up to
:data:`MAX_ACCOUNTS_PER_BROWSER` accounts, each signed in through its own
session row, and which of them is active. The cookie keeps its name and
attributes (``__Host-``, ``HttpOnly``, ``SameSite=Lax``, ``Path=/``).

Every sign-in rotates the cookie's value, so a value planted in the browser
before it (sign-in fixation) no longer names it. Switching does not: it adds
no account, and an open stream of the previous account ends at its next
re-check, when the cookie no longer resolves to that account. A value from
before browser sessions (a single account's ``cals1.`` session) still works
until the next sign-in, which revokes it and starts a browser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.accounts import tokens
from cloud_agent.accounts.config import LOCAL_SESSION_TTL_S
from cloud_agent.accounts.device_cookie import remember_device
from cloud_agent.accounts.session_cookie import (
    expire_local_session_cookies,
    local_session_from_cookies,
    set_local_session_cookie,
)
from cloud_agent.api._local_auth import local_accounts
from cloud_agent.application.accounts import identifier_key
from cloud_agent.ports.accounts import BrowserFull

if TYPE_CHECKING:
    from fastapi import Request, Response

    from cloud_agent.application.accounts import SignedIn
    from cloud_agent.domain.accounts import AccountRecord
    from cloud_agent.ports.accounts import BrowserState

#: How many accounts one browser may hold at once, as HybridInference allows.
MAX_ACCOUNTS_PER_BROWSER = 5


async def current_browser(request: Request) -> BrowserState | None:
    """The live browser the request's cookie names, read once per request."""
    kept = getattr(request.state, "local_browser", None)
    if kept is not None:
        return kept
    value = local_session_from_cookies(request.cookies)
    if not tokens.looks_like_browser_token(value):
        return None
    assert value is not None
    browser = await local_accounts(request).browser_state(tokens.browser_hash(value))
    request.state.local_browser = browser
    return browser


async def prepare_sign_in(request: Request, account_id: str | None) -> BrowserState | None:
    """The browser a sign-in joins, refused before any session exists if it is full.

    ``account_id`` is the account being signed in, when already known: an
    account signing in again takes its own place, so it never counts.

    Raises:
        BrowserFull: The browser holds the maximum of other accounts.
    """
    browser = await current_browser(request)
    if (
        browser is not None
        and browser.member(account_id) is None
        and len(browser.accounts) >= MAX_ACCOUNTS_PER_BROWSER
    ):
        raise BrowserFull(MAX_ACCOUNTS_PER_BROWSER)
    return browser


async def complete_sign_in(
    request: Request,
    response: Response,
    browser: BrowserState | None,
    signed_in: SignedIn,
    *,
    password_identifiers: list[str] | None = None,
) -> None:
    """Add a fresh session to the browser as its active account, and set the cookie.

    A browser that ended meanwhile (its last account signed out in another
    tab) is replaced by a new one. A browser that filled meanwhile refuses,
    and the session just opened is revoked. A password sign-in also gives the
    browser a device-cookie entry for the account.

    Raises:
        BrowserFull: Another tab filled the browser first.
    """
    accounts = local_accounts(request)
    new_token = tokens.new_browser_token()
    attached = False
    if browser is not None:
        try:
            attached = await accounts.attach_browser_account(
                browser_id=browser.id,
                account_id=signed_in.account.id,
                session_id=signed_in.session_id,
                max_accounts=MAX_ACCOUNTS_PER_BROWSER,
                new_token_hash=tokens.browser_hash(new_token),
                ttl_s=LOCAL_SESSION_TTL_S,
            )
        except BrowserFull:
            await accounts.revoke_session(
                tokens.session_hash(signed_in.token), reason="browser_full"
            )
            raise
    else:
        legacy = local_session_from_cookies(request.cookies)
        if tokens.looks_like_session_token(legacy):
            # A value naming no browser may be a single account's session from
            # before browsers: ended, not kept. (Revoking a value that names
            # nothing changes nothing.)
            assert legacy is not None
            await accounts.revoke_session(tokens.session_hash(legacy), reason="signed_out")
    if not attached:
        browser_id = tokens.new_session_id()
        await accounts.create_browser(
            browser_id=browser_id,
            token_hash=tokens.browser_hash(new_token),
            ttl_s=LOCAL_SESSION_TTL_S,
        )
        await accounts.attach_browser_account(
            browser_id=browser_id,
            account_id=signed_in.account.id,
            session_id=signed_in.session_id,
            max_accounts=MAX_ACCOUNTS_PER_BROWSER,
            new_token_hash=tokens.browser_hash(new_token),
            ttl_s=LOCAL_SESSION_TTL_S,
        )
    set_local_session_cookie(response, new_token)
    if password_identifiers:
        remember_device(response, password_identifiers, request.cookies)


def device_identifiers(account: AccountRecord) -> list[str]:
    """The sign-in limiter's keys of an account's password identifiers."""
    return [identifier_key(value) for value in (account.email, account.login_name) if value]


async def sign_out_of_browser(
    request: Request, response: Response, browser: BrowserState, account_id: str
) -> int:
    """Sign one account out of the browser; how many remain. The cookie goes with the last."""
    remaining = await local_accounts(request).detach_browser_account(
        browser_id=browser.id, account_id=account_id
    )
    if not remaining:
        expire_local_session_cookies(response)
    return remaining or 0


__all__ = [
    "MAX_ACCOUNTS_PER_BROWSER",
    "complete_sign_in",
    "current_browser",
    "device_identifiers",
    "prepare_sign_in",
    "sign_out_of_browser",
]
