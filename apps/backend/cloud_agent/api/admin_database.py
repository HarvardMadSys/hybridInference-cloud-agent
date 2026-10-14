"""Serve the operator's pgAdmin behind the current Agent admin permission.

The upstream is the deployment's own pgAdmin service, never a caller-selected
URL: Admin Settings choose among the services the deployment names and can
disable access, but cannot aim the proxy anywhere else. pgAdmin keeps its own
login and CSRF protection; neither an Agent credential nor an asserted user
header crosses the proxy. Each request uses a separate upstream cookie jar so
administrators cannot inherit one another's pgAdmin sessions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse

from cloud_agent import settings
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)
from cloud_agent.pgadmin_setup import initialize_pgadmin
from cloud_agent.settings import setting_env

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

router = APIRouter(
    prefix="/admin/agent", tags=["admin-database"], dependencies=[Depends(verify_admin_access)]
)

PGADMIN_PATH = "/admin/agent/pgadmin"
#: The service the ``pgadmin`` Compose profile runs; it needs no declaration.
BUNDLED_PGADMIN_URL = "http://pgadmin:5050"
#: Deployment variables that name another pgAdmin service. Saved settings never
#: reach the process environment, so only the operator can add one.
_DEPLOYMENT_PGADMIN_ENV = ("AGENT_PGADMIN_URL", "AGENT_PGADMIN_BOOTSTRAP_URL")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_SESSION_COOKIE = "pga4_session"
_COOKIES = {_SESSION_COOKIE, "remember_token"}

#: Added to every proxied response, whatever pgAdmin sends. pgAdmin is served
#: on the Agent's own origin and keeps its own (permissive) policy for its
#: inline scripts, so a script policy here would break it; these directives
#: are enforced alongside that one and break nothing pgAdmin does. No other
#: site may frame pgAdmin's SQL editor, and nothing it serves is sniffed into
#: a script or rendered as a plugin. :func:`_isolation` adds the second policy
#: that keeps pgAdmin's requests under its own path.
_SECURITY_HEADERS = (
    (b"content-security-policy", b"frame-ancestors 'self'; object-src 'none'; base-uri 'self'"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"x-content-type-options", b"nosniff"),
)
_REPLACED_HEADERS = {
    "cache-control",
    "cross-origin-opener-policy",
    "x-frame-options",
    "x-content-type-options",
}
_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "content-type",
    "origin",
    "referer",
    "x-pga-csrftoken",
    "x-requested-with",
}
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "server",
}


@dataclass(frozen=True)
class PgAdminConfig:
    """Validated deployment addresses, with no credentials."""

    upstream: httpx.URL
    public_url: str
    public_path: str
    host: str
    scheme: str


class DatabaseTools(BaseModel):
    """Whether the configured pgAdmin is reachable, and where to open it."""

    enabled: bool
    available: bool
    url: str | None = None
    setup_required: bool = False


class PgAdminSetup(BaseModel):
    """The first administrator chooses credentials for pgAdmin's own login."""

    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=12, max_length=256)

    @field_validator("email")
    @classmethod
    def safe_email(cls, value: str) -> str:
        """Keep the address compatible with the native container's initialization."""
        if "'" in value or "\\" in value:
            raise ValueError("Use an email address without quotes or backslashes.")
        return value

    @field_validator("password")
    @classmethod
    def single_line_password(cls, value: SecretStr) -> SecretStr:
        """A file-based bootstrap password must not lose trailing line breaks."""
        if any(character in value.get_secret_value() for character in "\r\n\0"):
            raise ValueError("Use a password without line breaks.")
        return value


def setup_root() -> Path | None:
    """Return only the operator-configured setup volume, never a browser path."""
    value = (os.environ.get("AGENT_PGADMIN_BOOTSTRAP_DIR") or "").strip()
    if not value:
        return None
    root = Path(value)
    if not root.is_absolute():
        raise HTTPException(status_code=503, detail="pgAdmin setup storage is unavailable.")
    return root


def _service(url: httpx.URL) -> tuple[str, str, int | None]:
    """Name the service a URL reaches by scheme, host and effective port.

    A trailing slash, letter case or a spelled-out default port does not make
    an address another service.
    """
    return url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme)


def deployment_pgadmin_services() -> set[tuple[str, str, int | None]]:
    """Return the pgAdmin services the deployment itself names.

    The Private pgAdmin URL is an admin-editable setting, and the proxy sends
    any method, path and body to it and returns the answer. Unbounded, that is
    a forward proxy from the control plane's network to a cloud metadata
    endpoint or any other private service it can reach. The environment is
    read directly, never through saved settings, because only the operator
    sets it.
    """
    services = set()
    for value in (BUNDLED_PGADMIN_URL, *map(os.environ.get, _DEPLOYMENT_PGADMIN_ENV)):
        try:
            if value and value.strip():
                services.add(_service(httpx.URL(value.strip())))
        except httpx.InvalidURL:
            continue
    return services


def pgadmin_config(*, bootstrap: bool = False) -> PgAdminConfig | None:
    """Refuse malformed configuration without returning its contents.

    Raises:
        HTTPException: 503 when an address is malformed, or when the saved
            upstream is not a pgAdmin service the deployment names.
    """
    upstream = (
        (
            os.environ.get("AGENT_PGADMIN_BOOTSTRAP_URL")
            if bootstrap
            else setting_env("AGENT_PGADMIN_URL")
        )
        or ""
    ).strip()
    if not upstream:
        return None
    public = (setting_env("AGENT_PGADMIN_PUBLIC_URL") or "").strip().rstrip("/")
    if bootstrap and not public:
        public = (os.environ.get("AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL") or "").strip().rstrip("/")
    if not public:
        web = (setting_env("AGENT_WEB_BASE_URL") or "").strip().rstrip("/")
        public = f"{web}/api{PGADMIN_PATH}" if web else ""
    try:
        origin = urlsplit(upstream)
        target = urlsplit(public)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or not target.path.endswith(PGADMIN_PATH)
            or target.query
            or target.fragment
        ):
            raise ValueError("invalid pgAdmin addresses")
        _ = origin.port, target.port
        config = PgAdminConfig(
            upstream=httpx.URL(upstream),
            public_url=public,
            public_path=target.path,
            host=target.netloc,
            scheme=target.scheme,
        )
    except (ValueError, httpx.InvalidURL):
        raise HTTPException(
            status_code=503,
            detail="Check AGENT_PGADMIN_URL and the public pgAdmin URL configuration.",
        ) from None
    # Compared as httpx parsed it, since that parse is the one the proxy dials.
    if _service(config.upstream) not in deployment_pgadmin_services():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Set Private pgAdmin URL to {BUNDLED_PGADMIN_URL}, or to a pgAdmin address "
                "set in AGENT_PGADMIN_URL or AGENT_PGADMIN_BOOTSTRAP_URL for this deployment."
            ),
        )
    return config


def pgadmin_client() -> httpx.AsyncClient:
    """Use the deployment's private network and leave long queries streaming."""
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(3600, connect=5, pool=5),
    )


async def pgadmin_available(config: PgAdminConfig) -> bool:
    """Check the fixed private service without sharing any browser credentials."""
    try:
        async with pgadmin_client() as client:
            ping = await client.get(config.upstream.copy_with(path="/misc/ping"), timeout=5)
            return ping.status_code == 200
    except httpx.HTTPError:
        return False


def _headers(request: Request, config: PgAdminConfig) -> dict[str, str]:
    headers = {name: value for name, value in request.headers.items() if name in _REQUEST_HEADERS}
    headers.update(
        {
            "host": config.host,
            "x-script-name": config.public_path,
            "x-scheme": config.scheme,
            "x-forwarded-proto": config.scheme,
            "accept-encoding": "identity",
        }
    )
    cookies: SimpleCookie = SimpleCookie()
    for name in _COOKIES:
        if name in request.cookies:
            cookies[name] = request.cookies[name]
    if cookies:
        headers["cookie"] = cookies.output(header="", sep=";").strip()
    return headers


def _cookie(header: str, config: PgAdminConfig) -> list[tuple[bytes, bytes]]:
    cookies: SimpleCookie = SimpleCookie()
    cookies.load(header)
    result = []
    for name, value in cookies.items():
        if name not in _COOKIES:
            continue
        value["path"] = f"{config.public_path}/"
        value["domain"] = ""
        value["httponly"] = True
        value["secure"] = config.scheme == "https"
        value["samesite"] = "Lax"
        result.append((b"set-cookie", value.OutputString().encode("latin-1")))
    return result


def _location(location: str, config: PgAdminConfig) -> str:
    parsed = urlsplit(location)
    if parsed.netloc and parsed.netloc not in {config.host, config.upstream.netloc.decode()}:
        return location
    if not parsed.path.startswith("/"):
        return location
    path = parsed.path
    if path != config.public_path and not path.startswith(f"{config.public_path}/"):
        path = f"{config.public_path}{path}"
    return urlunsplit(("", "", path, parsed.query, parsed.fragment))


def _isolation(config: PgAdminConfig) -> list[tuple[bytes, bytes]]:
    """Confine pgAdmin's pages to the pgAdmin path of the origin they share.

    pgAdmin is served on the Agent's own origin, where the administrator's
    session cookie authorizes every admin route, so a script running in a
    pgAdmin page could call them. This policy is enforced alongside pgAdmin's
    own: its fetches, form posts, frames and workers stay under the pgAdmin
    path, and COOP denies it a handle on a page it opens that does not send
    the same COOP, as the Agent's other pages do not. It narrows that reach;
    only a separate origin removes it.
    """
    host = config.host
    if host.isascii() and not host.startswith("["):
        # A CSP path ends at ";" or ",", so those are escaped.
        path = quote(config.public_path, safe="/%:@!$&'()*+=-._~")
        page = f"{config.scheme}://{host}{path}/"
        sockets = f"{'wss' if config.scheme == 'https' else 'ws'}://{host}{path}/"
        connect = f"{page} {sockets}"
    else:
        # A CSP host cannot spell an IP literal or an IDN; keep the origin bound.
        page = connect = "'self'"
    policy = (
        f"connect-src {connect} blob: data:; form-action {page}; "
        f"frame-src {page} blob: data:; worker-src {page} blob:"
    )
    return [
        (b"content-security-policy", policy.encode("latin-1")),
        (b"cross-origin-opener-policy", b"same-origin"),
    ]


def _refusal(status_code: int, detail: str) -> JSONResponse:
    """Answer for pgAdmin without becoming a page that could borrow the session.

    The proxy's own answers are served under the pgAdmin path, where a pgAdmin
    page may frame them. Sandboxed, such a document has an opaque origin and
    offers that page no same-origin window to make requests from.
    """
    return JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": "sandbox"},
    )


@router.get("/database-tools", response_model=DatabaseTools)
async def database_tools(
    response: Response,
    _admin: str = Depends(verify_admin_access),
) -> DatabaseTools:
    """Show a launch link only while the private service answers its health ping."""
    response.headers["Cache-Control"] = "no-store"
    config = pgadmin_config()
    root = setup_root()
    bootstrap = (
        pgadmin_config(bootstrap=True)
        if root is not None and not (root / "email").exists()
        else None
    )
    if config is None:
        if bootstrap is not None and not await pgadmin_available(bootstrap):
            return DatabaseTools(enabled=True, available=False, setup_required=True)
        return DatabaseTools(enabled=False, available=False)
    available = await pgadmin_available(config)
    return DatabaseTools(
        enabled=True,
        available=available,
        url=f"{config.public_url}/" if available else None,
        setup_required=bool(
            not available
            and bootstrap is not None
            and _service(bootstrap.upstream) == _service(config.upstream)
        ),
    )


@router.post("/database-tools/setup", response_model=DatabaseTools, status_code=202)
async def setup_pgadmin(
    body: PgAdminSetup,
    request: Request,
    response: Response,
    admin: str = Depends(verify_admin_access),
    store: Storage | None = Depends(get_storage),
) -> DatabaseTools:
    """Initialize once; existing accounts and password changes remain in pgAdmin."""
    response.headers["Cache-Control"] = "no-store"
    root, bootstrap = setup_root(), pgadmin_config(bootstrap=True)
    if bootstrap is None or root is None:
        raise HTTPException(status_code=503, detail="pgAdmin setup is not configured.")
    config = pgadmin_config()
    # Compared as services: the setting saved below drops a trailing slash
    # the bootstrap address may carry, and is still the same pgAdmin.
    if config is not None and _service(config.upstream) != _service(bootstrap.upstream):
        raise HTTPException(
            status_code=409, detail="A different pgAdmin service is configured in Admin Settings."
        )
    if (root / "email").exists():
        raise HTTPException(status_code=409, detail="pgAdmin has already been set up.")
    origin = request.headers.get("origin")
    if origin and origin != urlunsplit((bootstrap.scheme, bootstrap.host, "", "", "")):
        raise HTTPException(status_code=403, detail="Open pgAdmin setup from the admin dashboard.")
    if await pgadmin_available(bootstrap):
        raise HTTPException(status_code=409, detail="pgAdmin has already been set up.")
    # Saved before the volume is written, so a database failure leaves nothing
    # half done. If the write then fails, the saved connection names the same
    # service as the bootstrap address and the retry proceeds.
    if config is None:
        storage = require_store(store)
        changes = settings.encode_changes(
            {
                "pgadmin_url": str(bootstrap.upstream).rstrip("/"),
                "pgadmin_public_url": bootstrap.public_url,
            }
        )
        revision, encoded, _ = await storage.settings.update_settings(changes)
        settings.apply_settings(revision, encoded)
        await log_admin_action(
            storage,
            get_client_ip(request),
            "agent.settings.update",
            details={"revision": revision, "changes": changes},
            actor_id=admin,
            success=True,
        )
    try:
        await run_in_threadpool(
            initialize_pgadmin, root, str(body.email), body.password.get_secret_value()
        )
    except FileExistsError:
        raise HTTPException(status_code=409, detail="pgAdmin has already been set up.") from None
    except OSError:
        await log_admin_action(
            store,
            get_client_ip(request),
            "agent.database.pgadmin.setup",
            actor_id=admin,
            success=False,
        )
        raise HTTPException(
            status_code=503, detail="pgAdmin setup storage is unavailable."
        ) from None
    await log_admin_action(
        store, get_client_ip(request), "agent.database.pgadmin.setup", actor_id=admin, success=True
    )
    return DatabaseTools(enabled=True, available=False)


@router.get("/pgadmin", include_in_schema=False)
async def pgadmin_redirect(_admin: str = Depends(verify_admin_access)) -> RedirectResponse:
    """Normalize the launch URL before pgAdmin resolves relative paths."""
    config = pgadmin_config()
    if config is None:
        raise HTTPException(status_code=503, detail="pgAdmin is not configured.")
    return RedirectResponse(
        f"{config.public_url}/", status_code=307, headers={"Cache-Control": "no-store"}
    )


@router.api_route(
    "/pgadmin/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def pgadmin_proxy(
    path: str,
    request: Request,
    admin: str = Depends(verify_admin_access),
    store: Storage | None = Depends(get_storage),
) -> Response:
    """Authorize every pgAdmin page, asset, upload, and SQL request."""
    try:
        config = pgadmin_config()
    except HTTPException as refused:
        return _refusal(refused.status_code, refused.detail)
    if config is None:
        return _refusal(503, "pgAdmin is not configured.")
    try:
        # copy_with retains the configured host even for a path beginning with //.
        url = config.upstream.copy_with(path=f"/{path}", query=request.url.query.encode())
    except httpx.InvalidURL:
        # A control character, for one, cannot be sent upstream at all.
        return _refusal(404, "pgAdmin has no page at this address.")
    client = pgadmin_client()
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                url,
                headers=_headers(request, config),
                content=request.stream() if request.method not in {"GET", "HEAD"} else None,
            ),
            stream=True,
        )
    except httpx.HTTPError:
        await client.aclose()
        return _refusal(503, "pgAdmin is temporarily unavailable.")
    except BaseException:
        await client.aclose()
        raise

    async def close() -> None:
        try:
            await upstream.aclose()
        finally:
            await client.aclose()

    response = StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(close)
    )
    excluded = _HOP_HEADERS | {
        value.strip().lower() for value in upstream.headers.get("connection", "").split(",")
    }
    headers = []
    for name, value in upstream.headers.multi_items():
        # pgAdmin's own Content-Security-Policy is kept and enforced beside ours.
        if name in excluded or name in _REPLACED_HEADERS:
            continue
        if name == "set-cookie":
            headers.extend(_cookie(value, config))
        else:
            if name == "location":
                value = _location(value, config)
            headers.append((name.encode("latin-1"), value.encode("latin-1")))
    response.raw_headers = [
        *headers,
        (b"cache-control", b"no-store"),
        *_SECURITY_HEADERS,
        *_isolation(config),
    ]
    # The launch link opens the root, but a browser can enter pgAdmin at any
    # URL. A request with no pgAdmin session cookie is the first of a new
    # pgAdmin session wherever it lands, so that is recorded as well.
    new_session = _SESSION_COOKIE not in request.cookies
    if new_session or (not path and request.method == "GET"):
        await log_admin_action(
            store,
            get_client_ip(request),
            "agent.database.pgadmin.open",
            details={"pgadmin_session": "new" if new_session else "existing"},
            actor_id=admin,
            success=upstream.status_code < 400,
        )
    return response
