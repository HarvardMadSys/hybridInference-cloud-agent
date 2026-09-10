"""The session BFF — this service's own half of sign-in (task E8).

Sign-in happens at the gateway. This service owns the round trip to it, and
owns it **end to end on the server**: two browser-facing routes park a flow,
send the user to the gateway's consent page, and trade the returned code for a
session cookie.

- ``GET  /v1/session/login`` — mint a PKCE verifier and a CSRF ``state``, park
  both in a short-lived HttpOnly cookie, redirect to the gateway.
- ``GET  /v1/session/callback`` — validate ``state``, exchange the code here,
  verify the identity token, ask the gateway whether the account may sign in,
  set the session cookie, redirect into the app.
- ``POST /v1/session/logout`` — clear it.
- ``GET  /v1/session/me`` — who the cookie says we are, or 401.

**Why the exchange is here and not in the page.** An earlier revision had the
callback *page* redeem the code: the browser held the PKCE verifier in
``localStorage`` and the gateway's identity token in JavaScript, however
briefly. Both are bearer credentials, and "we call it a BFF" should mean the
browser never holds one. Here the verifier lives in an HttpOnly cookie no page
script can read, and the identity token never leaves this process.

**``state`` is generated and verified here, and nowhere else.** It is the
client's CSRF value, and a callback that accepts any ``code`` a victim's
browser can be made to arrive with is login CSRF — an attacker's account
silently occupying the victim's session, or the victim's code redeemed into the
attacker's. The parked flow is consumed on use, so a replayed callback finds
nothing.

**The callback is a ``GET``.** The gateway finishes by redirecting the browser
to ``redirect_uri?code=…``, and a redirect is a GET navigation.

**Identity and admission are separate.** The identity token proves who signs in;
the Gateway's current Agent access decision grants admission. Sessions retain
profile fields for compatibility, while every owner/admin request and ``/me``
reads current permissions. Account roles in tokens or profiles confer no Agent
permission.

It **fails closed**, like every other gateway call in this service: a gateway
that cannot be reached mints no session. That is a worse day for someone trying
to sign in and a much better one for everybody else, and it is the whole point
of AGENTS.md rule 4 — an unreachable authority means no permission, never
assumed permission.

**A 401 from ``/me`` means "not signed in".** A 503 means identity verification
or current Agent admission cannot be resolved. The browser shows a retry page
for that temporary failure instead of redirecting to sign-in.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from cloud_agent import identity
from cloud_agent.api.deps import (
    get_current_user,
    get_gateway_client,
    resolve_agent_access,
)
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import SessionUserResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/session")

#: Dev-only escape hatch. ``Secure`` is the default because a session cookie
#: sent over plaintext is a session cookie anyone on the path can take; a
#: developer running the whole stack on ``http://localhost`` is the one case
#: where that is not true, and they have to say so out loud.
ENV_COOKIE_SECURE = "AGENT_SESSION_COOKIE_SECURE"

#: The gateway's consent page, server-side. The web app had its own
#: ``NEXT_PUBLIC_`` copy while it built the authorize URL itself; the flow is
#: assembled here now, so this is the one that has to be set.
ENV_AUTHORIZE_URL = "GATEWAY_AUTHORIZE_URL"

#: The callback URL **as the gateway must see it**, verbatim.
#:
#: Configured rather than derived from the request, and that is not
#: fastidiousness: the gateway matches ``IDENTITY_ALLOWED_REDIRECTS`` as an
#: exact string, and deriving this from ``Host``/``X-Forwarded-*`` would let
#: whatever sits in front of this service decide where sign-in returns to.
ENV_REDIRECT_URI = "AGENT_SESSION_REDIRECT_URI"

#: Where the browser lands once the cookie is set — the web app's public origin
#: including any base path, e.g. ``https://example.org/agents``.
ENV_WEB_BASE_URL = "AGENT_WEB_BASE_URL"

#: This service's client id at the gateway.
OAUTH_CLIENT_ID = "cloud-agent"


def _cookie_is_secure() -> bool:
    """Whether the session cookie carries ``Secure``."""
    raw = (os.environ.get(ENV_COOKIE_SECURE) or "").strip().lower()
    return raw not in ("0", "false", "no")


def _env(name: str) -> str:
    """Return a trimmed environment value."""
    return (os.environ.get(name) or "").strip()


def safe_return_to(candidate: str | None) -> str:
    """Reduce a caller-supplied ``return_to`` to a same-site path.

    A ``return_to`` survives a round trip through another origin, so it is
    attacker-influenced: anything that is not a single-slash-rooted path — an
    absolute URL, a protocol-relative ``//evil.example``, a backslash the
    browser normalizes to one — becomes ``/``. That is what stops sign-in from
    being usable as an open redirect.

    Args:
        candidate: The requested landing path, if any.

    Returns:
        A path safe to append to this deployment's own web base URL.
    """
    if not candidate or not candidate.startswith("/"):
        return "/"
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return "/"
    return candidate


def _web_destination(return_to: str) -> str:
    """Return the absolute URL to land the browser on after sign-in.

    Args:
        return_to: An already-validated same-site path.

    Returns:
        The web base URL with the path appended, or the bare path when this
        deployment shares an origin and has configured no base URL.
    """
    base = _env(ENV_WEB_BASE_URL).rstrip("/")
    return f"{base}{return_to}" if base else return_to


#: Told to the app when this deployment cannot sign anyone in, so it can say so
#: instead of asking again. Without it the refusal is a redirect loop: the app
#: sends an unauthenticated visitor here, and here sends them straight back.
SIGN_IN_ERROR_PARAM = "sign_in_error"


def _sign_in_unavailable(reason: str) -> HTTPException:
    """Refuse a sign-in this deployment is not configured to perform.

    503 rather than 500: the caller did nothing wrong, and retrying with
    another credential will not help.
    """
    return HTTPException(status_code=503, detail=reason)


def _unavailable_response(reason: str) -> RedirectResponse:
    """Send an unsignable-in visitor back to the app with something to show.

    A misconfigured deployment is an operator's problem, but the person in
    front of it still deserves a sentence rather than a raw 503 body. When no
    web base URL is configured there is nowhere to send them, and the 503 is
    the honest answer.

    Args:
        reason: What is missing, for the log.

    Returns:
        A redirect carrying :data:`SIGN_IN_ERROR_PARAM`.

    Raises:
        HTTPException: 503 when this deployment has no app to redirect to.
    """
    logger.warning(
        "agent_sign_in_unconfigured",
        extra={"event": "agent_sign_in_unconfigured", "reason": reason},
    )
    if not _env(ENV_WEB_BASE_URL):
        raise _sign_in_unavailable(reason)
    # `/login` rather than the app root: that page is the one that can say what
    # is wrong, and it is reachable without a session. This is the one route of
    # the web app this service names, and it names it for that reason.
    return RedirectResponse(
        f"{_web_destination('/login')}?{urlencode({SIGN_IN_ERROR_PARAM: 'unavailable'})}",
        status_code=303,
    )


def _callback_error_response(error: HTTPException, return_to: str) -> RedirectResponse:
    """Render account denial or temporary failure in the public login page."""
    if error.status_code not in (403, 503) or not _env(ENV_WEB_BASE_URL):
        raise error
    reason = "account_unavailable" if error.status_code == 403 else "temporary"
    query = urlencode({SIGN_IN_ERROR_PARAM: reason, "return_to": safe_return_to(return_to)})
    response = RedirectResponse(f"{_web_destination('/login')}?{query}", status_code=303)
    response.delete_cookie(
        identity.LOGIN_FLOW_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(),
    )
    return response


@router.get("/login")
async def session_login(
    return_to: str = Query(default="/"),
) -> RedirectResponse:
    """Start a sign-in: park the PKCE flow and redirect to the gateway.

    Args:
        return_to: Where in the app to land afterwards. Reduced to a same-site
            path before it is parked.

    Returns:
        A redirect to the gateway's consent page.

    Raises:
        HTTPException: 503 if this deployment cannot sign anyone in.
    """
    authorize_url = _env(ENV_AUTHORIZE_URL)
    redirect_uri = _env(ENV_REDIRECT_URI)
    if not authorize_url or not redirect_uri:
        return _unavailable_response(
            f"{ENV_AUTHORIZE_URL} and {ENV_REDIRECT_URI} must both be set to sign anyone in."
        )

    verifier = identity.new_verifier()
    state = identity.new_state()
    try:
        flow = identity.mint_login_flow(
            verifier=verifier, state=state, return_to=safe_return_to(return_to)
        )
    except identity.IdentityNotConfigured as exc:
        raise _sign_in_unavailable(str(exc)) from None

    query = urlencode(
        {
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code_challenge": identity.challenge_for(verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    separator = "&" if "?" in authorize_url else "?"
    # 303, not 302: this is a redirect to a different resource after a request
    # that is itself not the destination, and 303 is unambiguous about the
    # browser issuing a GET.
    response = RedirectResponse(f"{authorize_url}{separator}{query}", status_code=303)
    response.set_cookie(
        identity.LOGIN_FLOW_COOKIE,
        flow,
        max_age=identity.LOGIN_FLOW_TTL_S,
        httponly=True,
        # Lax, not Strict: the return leg is a top-level navigation back from
        # the gateway, and Strict would drop the cookie on exactly that
        # request — the one it exists for.
        samesite="lax",
        secure=_cookie_is_secure(),
        path="/",
    )
    return response


@router.get("/callback")
async def session_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    gateway=Depends(get_gateway_client),
) -> RedirectResponse:
    """Finish a sign-in: validate, exchange, and set the session cookie.

    Args:
        request: The incoming request, for the parked login-flow cookie.
        code: The one-time authorization code the gateway sent back.
        state: The CSRF value the gateway echoed.
        gateway: The gateway's contracts.

    Returns:
        A redirect into the app with a session, or to the public login page
        explaining account denial or temporary failure when a web URL is set.

    Raises:
        HTTPException: 400 if the flow is missing, mismatched, replayed or
            unredeemable, 403 if the account may not sign in, 503 if this
            deployment cannot verify identity or could not reach the gateway.
    """
    redirect_uri = _env(ENV_REDIRECT_URI)
    if not redirect_uri:
        raise _sign_in_unavailable(f"{ENV_REDIRECT_URI} must be set to sign anyone in.")

    try:
        flow = identity.verify_login_flow(request.cookies.get(identity.LOGIN_FLOW_COOKIE) or "")
    except identity.IdentityNotConfigured as exc:
        raise _sign_in_unavailable(str(exc)) from None
    except identity.IdentityError:
        raise HTTPException(
            status_code=400,
            detail="This sign-in has expired. Please start again.",
        ) from None

    # Checked before the code is spent: a callback naming a state this service
    # did not mint is not a callback worth redeeming.
    if not identity.state_matches(state, str(flow.get("state") or "")):
        raise HTTPException(
            status_code=400,
            detail="This sign-in response did not match the request that started it.",
        )
    if not code:
        raise HTTPException(status_code=400, detail="The gateway returned no authorization code.")

    try:
        identity_token = await gateway.exchange_authorization_code(
            code=code,
            verifier=str(flow.get("verifier") or ""),
            client_id=OAUTH_CLIENT_ID,
            redirect_uri=redirect_uri,
        )
    except GatewayError as exc:
        logger.warning(
            "agent_session_code_exchange_failed",
            exc_info=True,
            extra={"event": "agent_session_code_exchange_failed"},
        )
        # Three different answers, because they ask for three different things.
        # A 403 is the gateway's verdict on the *account* — it re-reads status
        # at exchange time precisely so a suspension between consent and here
        # takes effect at once — and "please start again" would send that
        # person round the loop forever. Any other refusal is about the code
        # (stale, replayed, mismatched), and starting again is the fix. An
        # unreachable gateway is this deployment's problem, not theirs.
        if exc.status_code == 403:
            return _callback_error_response(
                HTTPException(status_code=403, detail="This account cannot sign in."),
                str(flow.get("return_to") or "/"),
            )
        status_code = 400 if exc.definitive else 503
        error = HTTPException(
            status_code=status_code,
            detail=(
                "This sign-in could not be completed. Please start again."
                if status_code == 400
                else "Cannot reach the gateway to complete sign-in; try again shortly."
            ),
        )
        return _callback_error_response(error, str(flow.get("return_to") or "/"))

    try:
        session, user = await _establish_session(identity_token, gateway)
    except HTTPException as exc:
        return _callback_error_response(exc, str(flow.get("return_to") or "/"))

    response = RedirectResponse(
        _web_destination(safe_return_to(str(flow.get("return_to") or "/"))), status_code=303
    )
    _set_session_cookie(response, session)
    # Consumed on use: a replayed callback finds nothing parked, so a code that
    # failed to exchange cannot be retried against the same verifier.
    response.delete_cookie(
        identity.LOGIN_FLOW_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(),
    )
    logger.info(
        "agent_session_established",
        extra={"event": "agent_session_established", "user_id": user["id"]},
    )
    return response


async def _establish_session(identity_token: str, gateway: Any) -> tuple[str, dict[str, Any]]:
    """Verify an identity token and mint this service's session from it.

    Args:
        identity_token: The gateway's RS256 identity token.
        gateway: The gateway's contracts, for the account's status.

    Returns:
        The signed session, and the user record the routes answer with.

    Raises:
        HTTPException: 401 if the identity token does not verify, 403 if the
            gateway says this account may not sign in, 503 if this deployment
            cannot verify identity or could not reach the gateway.
    """
    try:
        claims = identity.verify_identity_token(identity_token)
    except identity.IdentityNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except identity.IdentityError:
        # Deliberately not echoing the reason. "Signed by an unknown key" and
        # "expired" are the same answer to a browser, and the difference is
        # useful mostly to somebody probing.
        raise HTTPException(status_code=401, detail="Invalid identity token.") from None

    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid identity token.")

    decision = await resolve_agent_access(gateway, user_id)
    try:
        status = await gateway.user_status(user_id)
    except GatewayError as exc:
        logger.warning(
            "agent_session_status_unavailable",
            exc_info=True,
            extra={"event": "agent_session_status_unavailable", "user_id": user_id},
        )
        raise HTTPException(
            status_code=503,
            detail="Cannot confirm this account with the gateway; try again shortly.",
        ) from exc

    # A valid identity without Agent admission may still see the access-required
    # page, but a missing or suspended account must never receive a session.
    if status.get("exists") is not True or status.get("active") is not True:
        raise HTTPException(status_code=403, detail="This account cannot sign in.")

    # Retain the existing profile fields for compatibility. They never grant
    # Agent permissions; only the access decision above can do that.
    role = str(status.get("role") or "free")
    email = status.get("email") or claims.get("email")
    email = str(email) if email else None
    # Read from either source and then **minted into the session**, because the
    # session is the only thing ``/me`` can read. Sourcing it here and returning
    # it without minting it is what made the two routes disagree: login answered
    # with a name and ``/me`` answered ``null`` for the same signed-in person,
    # for the whole life of the cookie. Today no gateway contract reports a name
    # at all, so both answer ``null`` — but they now do so for the same reason,
    # and a gateway that grows the field is answered consistently without a
    # second change here.
    user_name = status.get("user_name") or claims.get("user_name")
    user_name = str(user_name) if user_name else None

    try:
        session = identity.mint_session(
            user_id=user_id, email=email, role=role, user_name=user_name
        )
    except identity.IdentityNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    return session, {
        "id": user_id,
        "email": email,
        "role": role,
        "user_name": user_name,
        "agent_access": decision.public().model_dump(),
    }


def _set_session_cookie(response: Response, session: str) -> None:
    """Attach the session cookie to a response.

    Args:
        response: The response to set the cookie on.
        session: The signed session token.
    """
    response.set_cookie(
        identity.SESSION_COOKIE,
        session,
        max_age=identity.SESSION_TTL_S,
        httponly=True,
        # Lax, not Strict: the return leg of sign-in is a top-level navigation
        # back from the gateway, and Strict would drop the cookie on exactly
        # that request. Lax still refuses to send it on a cross-site POST,
        # which is the request a CSRF would have to be.
        samesite="lax",
        secure=_cookie_is_secure(),
        path="/",
    )


@router.post("/logout")
async def session_logout(response: Response) -> dict[str, bool]:
    """Clear the session cookie.

    Unauthenticated on purpose: signing out must work when the session is
    already expired or malformed, which is exactly when a browser most wants
    it. There is nothing to protect — the only effect is deleting a cookie the
    caller already holds.

    The attributes must match the ones the cookie was set with, or the browser
    keeps the original and the user stays signed in.
    """
    response.delete_cookie(
        identity.SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(),
    )
    return {"ok": True}


@router.get("/me", response_model=SessionUserResponse)
async def session_me(
    request: Request, gateway: Any = Depends(get_gateway_client)
) -> SessionUserResponse:
    """Return session identity and a fresh Agent access decision.

    Existing sessions whose access was revoked remain identifiable with a
    denied decision, so the UI can explain the refusal without a login loop.
    An unavailable authority returns 503, never cached permission.
    """
    user = await get_current_user(
        request=request, authorization=request.headers.get("authorization")
    )
    decision = await resolve_agent_access(gateway, user["user_id"])
    return SessionUserResponse(
        id=user["user_id"],
        email=user.get("email"),
        role=user["role"],
        user_name=user.get("user_name"),
        agent_access=decision.public(),
    )
