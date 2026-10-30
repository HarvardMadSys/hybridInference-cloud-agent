"""The signed-in browser's session: who it is, and signing it out.

Sign-in, sign-up and the account chooser are the account routes
(``/v1/auth/*``). This router keeps the two session reads every page shares:

- ``GET  /v1/session/me``: the active account and a fresh Agent access
  decision, or 401.
- ``POST /v1/session/logout``: end the active account's session and clear its
  cookie when it was the browser's last.

**Identity and admission are separate.** The session proves who is signed in;
the account's current permissions, read just now, grant admission. A session
whose access was revoked stays identifiable with a denied decision, so the app
can explain the refusal without a sign-in loop.

**A 401 from ``/me`` means "not signed in".** A 503 means the session or the
account's admission cannot be read just now; the app offers to retry without
signing in again.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cloud_agent.api.deps import (
    get_current_user,
    get_owner_directory,
    resolve_agent_access,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import SessionUserResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/session")


@router.post("/logout")
async def session_logout(request: Request, response: Response) -> dict[str, bool]:
    """End the browser's active session, then clear the cookie with its last account.

    Unauthenticated on purpose: signing out must work when the session is
    already expired or malformed, which is exactly when a browser most wants
    it.

    The session is a server-side row, revoked before the cookie is cleared, so
    a copy of the cookie stops working too. A store that cannot revoke it
    answers 503 rather than claiming the session ended.
    """
    await _end_local_session(request, response)
    return {"ok": True}


async def _end_local_session(request: Request, response: Response) -> None:
    """Sign the browser's active account out; the cookie goes with its last account.

    Other accounts signed in on the browser stay signed in, with none active,
    as in HybridInference. A cookie that names no live browser, or a
    single-account session from before browsers, is ended and cleared; one
    without the session's shape is only cleared.
    """
    from cloud_agent.accounts.session_cookie import (
        expire_local_session_cookies,
        presented_local_token,
    )
    from cloud_agent.accounts.tokens import looks_like_browser_token
    from cloud_agent.api._local_auth import local_flows
    from cloud_agent.api._local_browser import current_browser, sign_out_of_browser
    from cloud_agent.api.deps import bearer_token

    token = presented_local_token(
        request.cookies, bearer_token(request.headers.get("authorization"))
    )
    if not looks_like_browser_token(token):
        # Nothing of the session's shape names any session or browser: there
        # is nothing to end, only a cookie to clear.
        expire_local_session_cookies(response)
        return
    try:
        browser = await current_browser(request)
        if browser is not None:
            if browser.active_account_id is not None:
                await sign_out_of_browser(request, response, browser, browser.active_account_id)
            return
        await local_flows(request).sign_out(token)
    except Exception:
        logger.warning(
            "agent_local_sign_out_failed",
            exc_info=True,
            extra={"event": "agent_local_sign_out_failed"},
        )
        raise HTTPException(
            status_code=503, detail="Could not end the session; try again shortly."
        ) from None
    expire_local_session_cookies(response)


async def _local_me(request: Request, owners: Any, user_id: str) -> SessionUserResponse:
    """The account, its current access and its application, all read just now."""
    from cloud_agent.api._local_auth import local_session_user
    from cloud_agent.api.deps import local_session_owner

    owner = await local_session_owner(request, request.headers.get("authorization"))
    if owner is None or owner.account.id != user_id:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")
    decision = await resolve_agent_access(owners, user_id)
    return local_session_user(owner.account, decision)


@router.get("/me", response_model=SessionUserResponse, response_model_exclude_none=True)
async def session_me(
    request: Request, owners: Any = Depends(get_owner_directory)
) -> SessionUserResponse:
    """Return session identity and a fresh Agent access decision.

    Existing sessions whose access was revoked remain identifiable with a
    denied decision, so the UI can explain the refusal without a login loop.
    An unreadable account store returns 503, never cached permission.
    """
    user = await get_current_user(
        request=request, authorization=request.headers.get("authorization")
    )
    return await _local_me(request, owners, user["user_id"])
