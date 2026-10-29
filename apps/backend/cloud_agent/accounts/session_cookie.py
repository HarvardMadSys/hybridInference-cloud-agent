"""Where a local session travels: its own cookie, or a bearer of its own shape.

Gateway-mode cookies (``__Host-cloud_agent_session``) are never read in local
mode, and a Gateway-mode session presented as a bearer is not the local
shape, so it is refused before any lookup. The cookie follows the same rules
as the Gateway-mode one: ``__Host-`` wherever it is ``Secure``, ``HttpOnly``,
``SameSite=Lax``, ``Path=/``, and the plain development name is read only
where the cookie is not ``Secure``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.accounts.config import (
    LOCAL_SESSION_COOKIE,
    LOCAL_SESSION_COOKIE_INSECURE,
    local_session_ttl_s,
)
from cloud_agent.accounts.tokens import looks_like_session_token
from cloud_agent.identity import session_cookie_is_secure

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.responses import Response


def local_cookie_name(*, secure: bool) -> str:
    """The name a new local session cookie is set under."""
    return LOCAL_SESSION_COOKIE if secure else LOCAL_SESSION_COOKIE_INSECURE


def local_session_from_cookies(cookies: Mapping[str, str]) -> str | None:
    """The local session value a request's cookies carry, if any."""
    if LOCAL_SESSION_COOKIE in cookies:
        return cookies[LOCAL_SESSION_COOKIE] or None
    if session_cookie_is_secure():
        return None
    return cookies.get(LOCAL_SESSION_COOKIE_INSECURE) or None


def presented_local_token(cookies: Mapping[str, str], bearer: str | None) -> str | None:
    """The cookie if present, else a bearer of the local session's shape."""
    cookie = local_session_from_cookies(cookies)
    if cookie:
        return cookie
    return bearer if looks_like_session_token(bearer) else None


def set_local_session_cookie(response: Response, token: str) -> None:
    """Attach a new local session cookie."""
    secure = session_cookie_is_secure()
    response.set_cookie(
        local_cookie_name(secure=secure),
        token,
        max_age=local_session_ttl_s(),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def expire_local_session_cookies(response: Response) -> None:
    """Tell the browser to drop both local cookie names it may hold."""
    secure = session_cookie_is_secure()
    names = (
        (LOCAL_SESSION_COOKIE, LOCAL_SESSION_COOKIE_INSECURE)
        if secure
        else (LOCAL_SESSION_COOKIE_INSECURE,)
    )
    for name in names:
        response.delete_cookie(name, path="/", httponly=True, samesite="lax", secure=secure)


__all__ = [
    "expire_local_session_cookies",
    "local_cookie_name",
    "local_session_from_cookies",
    "presented_local_token",
    "set_local_session_cookie",
]
