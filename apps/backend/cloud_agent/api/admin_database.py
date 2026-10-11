"""Serve the operator's pgAdmin behind the current Agent admin permission.

The upstream is a private service, never a caller-selected URL. pgAdmin keeps
its own login and CSRF protection; neither an Agent credential nor an asserted
user header crosses the proxy. Each request uses a separate upstream cookie
jar so administrators cannot inherit one another's pgAdmin sessions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, StreamingResponse

from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    verify_admin_access,
)

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

router = APIRouter(
    prefix="/admin/agent", tags=["admin-database"], dependencies=[Depends(verify_admin_access)]
)

PGADMIN_PATH = "/admin/agent/pgadmin"
_COOKIES = {"pga4_session", "remember_token"}
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


def pgadmin_config() -> PgAdminConfig | None:
    """Refuse malformed configuration without returning its contents."""
    upstream = (os.environ.get("AGENT_PGADMIN_URL") or "").strip()
    if not upstream:
        return None
    public = (os.environ.get("AGENT_PGADMIN_PUBLIC_URL") or "").strip().rstrip("/")
    if not public:
        web = (os.environ.get("AGENT_WEB_BASE_URL") or "").strip().rstrip("/")
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
        return PgAdminConfig(
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


def pgadmin_client() -> httpx.AsyncClient:
    """Use the deployment's private network and leave long queries streaming."""
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(3600, connect=5, pool=5),
    )


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


@router.get("/database-tools", response_model=DatabaseTools)
async def database_tools(
    response: Response,
    _admin: str = Depends(verify_admin_access),
) -> DatabaseTools:
    """Show a launch link only while the private service answers its health ping."""
    response.headers["Cache-Control"] = "no-store"
    config = pgadmin_config()
    if config is None:
        return DatabaseTools(enabled=False, available=False)
    try:
        async with pgadmin_client() as client:
            ping = await client.get(config.upstream.copy_with(path="/misc/ping"), timeout=5)
            available = ping.status_code == 200
    except httpx.HTTPError:
        available = False
    return DatabaseTools(
        enabled=True, available=available, url=f"{config.public_url}/" if available else None
    )


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
) -> StreamingResponse:
    """Authorize every pgAdmin page, asset, upload, and SQL request."""
    config = pgadmin_config()
    if config is None:
        raise HTTPException(status_code=503, detail="pgAdmin is not configured.")
    # copy_with retains the configured host even for a path beginning with //.
    url = config.upstream.copy_with(path=f"/{path}", query=request.url.query.encode())
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
        raise HTTPException(status_code=503, detail="pgAdmin is temporarily unavailable.") from None
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
        if name in excluded or name == "cache-control":
            continue
        if name == "set-cookie":
            headers.extend(_cookie(value, config))
        else:
            if name == "location":
                value = _location(value, config)
            headers.append((name.encode("latin-1"), value.encode("latin-1")))
    response.raw_headers = [*headers, (b"cache-control", b"no-store")]
    if not path and request.method == "GET":
        await log_admin_action(
            store,
            get_client_ip(request),
            "agent.database.pgadmin.open",
            actor_id=admin,
            success=upstream.status_code < 400,
        )
    return response
