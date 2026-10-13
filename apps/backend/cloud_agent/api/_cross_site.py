"""Refuse cross-site writes that ride on the session cookie.

The session cookie is ``SameSite=Lax``, which keeps it off a cross-*site*
POST but not off a same-*site* one, and the shipped topology puts the web app
on a site it shares with the gateway and often with sibling hosts. A page on
any of them could make a browser send a state-changing request here with the
session attached: a job cancelled, a conversation archived, a setting saved.

Browsers say where a request came from, and that is what this checks, in the
order Go's ``net/http.CrossOriginProtection`` uses:

- A safe method (GET, HEAD, OPTIONS) passes; no route changes state on one.
- ``Sec-Fetch-Site: same-origin`` passes, and so does ``none``, a request the
  user started themselves. Any other value is refused unless ``Origin`` is one
  this deployment serves its web app or pgAdmin from.
- Without ``Sec-Fetch-Site`` (an older browser, or plain HTTP away from
  localhost), ``Origin`` must be one of those origins or the request's own
  ``Host`` over HTTPS (or over HTTP too where the cookie is not ``Secure``).
- With neither header the request did not come from a page — browsers send
  ``Origin`` on every cross-origin POST — and passes, as scripts and tests must.

Only requests carrying the session cookie, under either name, are checked. A
bearer credential is not ambient: a page cannot make a browser attach one. So
hosts, workers, the on-call relay and every other machine caller are untouched,
and so is sign-in's callback, which is a GET.

A plain ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``: it
answers or passes before a response starts and never wraps one, so the
long-lived event streams behind it are not buffered or re-framed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse

from cloud_agent import identity
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import setting_env

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = get_logger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: ``Sec-Fetch-Site`` values that name this origin, or no page at all.
_FIRST_PARTY = frozenset({"same-origin", "none"})

#: The configured addresses a page of this deployment is served from. Read per
#: request, because both are saved settings an administrator can change.
_TRUSTED_ORIGIN_SETTINGS = ("AGENT_WEB_BASE_URL", "AGENT_PGADMIN_PUBLIC_URL")

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: ``api.session.ENV_COOKIE_SECURE``, named here to keep this module free of
#: the session routes' imports.
_COOKIE_SECURE_SETTING = "AGENT_SESSION_COOKIE_SECURE"


def _origin(url: str) -> str | None:
    """Reduce a URL to the origin a browser would send for it, or ``None``."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


def trusted_origins() -> set[str]:
    """Return the origins this deployment's own pages are served from."""
    origins = set()
    for name in _TRUSTED_ORIGIN_SETTINGS:
        origin = _origin(setting_env(name) or "")
        if origin is not None:
            origins.add(origin)
    return origins


def first_party(headers: Headers) -> bool:
    """Whether a browser request came from this deployment's own pages.

    Args:
        headers: The request's headers.

    Returns:
        False only when the browser said the request came from somewhere
        else, or named an origin this deployment does not serve.
    """
    site = headers.get("sec-fetch-site", "").strip().lower()
    if site in _FIRST_PARTY:
        return True
    origin = headers.get("origin", "").strip()
    if not site and not origin:
        return True
    if origin and _origin(origin) in trusted_origins():
        return True
    if site or not origin:
        return False
    host = headers.get("host", "").strip()
    presented = _origin(origin)
    if not host or presented is None:
        return False
    # The whole origin, not just its authority: a browser that sends no
    # ``Sec-Fetch-Site`` also treats ``http://`` and ``https://`` of one host
    # as one site, and would attach the HTTPS session to a write from the
    # plain-HTTP page. The request's own scheme is not trustworthy behind a
    # TLS-terminating proxy, so a ``Secure`` cookie, which only an HTTPS
    # deployment holds, is what says the page must be HTTPS.
    schemes = ("https",) if _session_cookie_is_secure() else ("https", "http")
    return presented in {_origin(f"{scheme}://{host}") for scheme in schemes}


def _session_cookie_is_secure() -> bool:
    """Whether the session cookie carries ``Secure``, as ``api.session`` sets it."""
    raw = (setting_env(_COOKIE_SECURE_SETTING) or "").strip().lower()
    return raw not in ("0", "false", "no")


def _carries_session(headers: Headers) -> bool:
    """Whether the request sends the session cookie under either of its names."""
    raw = "; ".join(headers.getlist("cookie"))
    return bool(raw) and identity.session_from_cookies(cookie_parser(raw)) is not None


class CrossSiteWriteGuard:
    """Refuse a cookie-authenticated unsafe request a browser says came from elsewhere."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application this guards."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, or answer 403 before it reaches any route."""
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not _carries_session(headers) or first_party(headers):
            await self.app(scope, receive, send)
            return
        logger.warning(
            "agent_cross_site_request_refused",
            extra={
                "event": "agent_cross_site_request_refused",
                "method": scope["method"],
                "path": scope.get("path", ""),
                "fetch_site": headers.get("sec-fetch-site", "") or "absent",
            },
        )
        response = JSONResponse(
            status_code=403,
            content={
                "detail": {
                    "error": {
                        "type": "cross_site_request",
                        "message": "This request came from another site and was refused.",
                    }
                }
            },
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
