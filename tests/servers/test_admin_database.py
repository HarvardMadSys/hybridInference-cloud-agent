"""The pgAdmin proxy authorizes every request and isolates credentials."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_database
from cloud_agent.api.deps import verify_admin_access
from cloud_agent.app import install_admin_no_store

ADMIN = {"Authorization": "Bearer test-database-admin"}
ROOT = "/admin/agent/pgadmin"
PUBLIC = f"https://agents.example.org/agents/api{ROOT}"


@pytest.fixture
async def database_api(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-database-admin")
    monkeypatch.setenv("AGENT_PGADMIN_URL", "http://pgadmin:5050")
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://agents.example.org/agents/")
    monkeypatch.delenv("AGENT_PGADMIN_PUBLIC_URL", raising=False)
    app = FastAPI()
    audit = AsyncMock()
    app.state.storage = SimpleNamespace(audit=audit)
    app.include_router(admin_database.router)
    install_admin_no_store(app)
    seen = []
    clients = []
    result = {"status": 200, "headers": [], "content": b"PING", "error": None}

    async def answer(request: httpx.Request):
        await request.aread()
        seen.append(request)
        if result["error"]:
            raise result["error"]
        return httpx.Response(
            result["status"],
            headers=result["headers"],
            stream=httpx.ByteStream(result["content"]),
        )

    def upstream_client():
        client = AsyncClient(transport=httpx.MockTransport(answer))
        clients.append(client)
        return client

    monkeypatch.setattr(admin_database, "pgadmin_client", upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client, seen, result, audit, clients, app


@pytest.mark.parametrize(
    "path", ["/database-tools", "/pgadmin", "/pgadmin/", "/pgadmin/static/app.js"]
)
async def test_every_entry_point_requires_admin_before_contacting_pgadmin(database_api, path):
    client, seen, _, _, _, _ = database_api
    response = await client.get(f"/admin/agent{path}")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert seen == []


async def test_the_status_checks_health_and_returns_only_the_public_url(database_api):
    client, seen, _, _, clients, _ = database_api
    response = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert response.json() == {"enabled": True, "available": True, "url": f"{PUBLIC}/"}
    assert seen[0].url == "http://pgadmin:5050/misc/ping"
    assert "authorization" not in seen[0].headers
    assert response.headers["cache-control"] == "no-store"
    assert clients[0].is_closed


async def test_an_unconfigured_service_has_no_launch_link(database_api, monkeypatch):
    client, seen, _, _, _, _ = database_api
    monkeypatch.delenv("AGENT_PGADMIN_URL")
    response = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert response.json() == {"enabled": False, "available": False, "url": None}
    assert (await client.get(f"{ROOT}/", headers=ADMIN)).status_code == 503
    assert seen == []


async def test_an_unavailable_service_has_no_launch_link_and_no_private_error_details(database_api):
    client, _, result, _, clients, _ = database_api
    result["error"] = httpx.ConnectError("private service diagnostics")
    status = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert status.json() == {"enabled": True, "available": False, "url": None}
    proxy = await client.get(f"{ROOT}/", headers=ADMIN)
    assert proxy.status_code == 503
    assert "private service diagnostics" not in proxy.text
    assert all(client.is_closed for client in clients)


async def test_proxy_streams_form_body_query_and_csrf_without_agent_credentials(database_api):
    client, seen, result, _, clients, _ = database_api
    result["headers"] = [("content-type", "application/json")]
    result["content"] = b'{"success": 1}'
    response = await client.post(
        f"{ROOT}/query_tool/run?sid=7&query=a%2Bb",
        headers={
            **ADMIN,
            "Cookie": "cloud_agent_session=agent-secret; pga4_session=pgadmin-session; other=secret",
            "X-pgA-CSRFToken": "csrf-proof",
            "X-Forwarded-User": "spoofed-admin",
            "X-Script-Name": "/spoofed",
            "Origin": "https://agents.example.org",
            "Content-Type": "application/json",
        },
        content=b'{"sql":"select 1"}',
    )
    assert response.json() == {"success": 1}
    request = seen[0]
    assert request.url == "http://pgadmin:5050/query_tool/run?sid=7&query=a%2Bb"
    assert request.content == b'{"sql":"select 1"}'
    assert request.headers["x-pga-csrftoken"] == "csrf-proof"
    assert request.headers["cookie"] == "pga4_session=pgadmin-session"
    assert request.headers["host"] == "agents.example.org"
    assert request.headers["x-script-name"] == f"/agents/api{ROOT}"
    assert request.headers["x-scheme"] == "https"
    assert "authorization" not in request.headers
    assert "x-forwarded-user" not in request.headers
    assert response.headers["cache-control"] == "no-store"
    assert clients[0].is_closed


async def test_cookie_scope_and_redirect_keep_the_public_proxy_prefix(database_api):
    client, _, result, audit, _, _ = database_api
    result["status"] = 302
    result["headers"] = [
        ("location", "http://pgadmin:5050/login?next=%2Fbrowser"),
        ("set-cookie", "pga4_session=session; Path=/; Domain=pgadmin"),
        ("set-cookie", "remember_token=remembered; Path=/"),
        ("set-cookie", "cloud_agent_session=never-accept-this; Path=/"),
        ("cache-control", "public, max-age=3600"),
    ]
    response = await client.get(f"{ROOT}/", headers=ADMIN)
    assert response.headers["location"] == f"/agents/api{ROOT}/login?next=%2Fbrowser"
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for cookie in cookies:
        assert f"Path=/agents/api{ROOT}/" in cookie
        assert "Domain=" not in cookie
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    assert "cloud_agent_session" not in " ".join(cookies)
    assert response.headers["cache-control"] == "no-store"
    audit.record_admin_action.assert_awaited_once()
    assert audit.record_admin_action.call_args.kwargs["actor_id"] == "admin-token"


async def test_pgadmin_sessions_are_separate_for_each_request(database_api):
    client, seen, result, _, clients, _ = database_api
    result["headers"] = [("set-cookie", "pga4_session=admin-a; Path=/")]
    await client.get(f"{ROOT}/browser", headers=ADMIN)
    await client.get(f"{ROOT}/browser", headers={**ADMIN, "Cookie": "pga4_session=admin-b"})
    assert "cookie" not in seen[0].headers
    assert seen[1].headers["cookie"] == "pga4_session=admin-b"
    assert clients[0] is not clients[1]
    assert all(client.is_closed for client in clients)


async def test_permission_is_checked_again_after_the_launch(database_api):
    client, seen, _, _, _, app = database_api
    await client.get(f"{ROOT}/", headers=ADMIN)

    async def revoked():
        raise HTTPException(status_code=403, detail="Admin permission revoked")

    app.dependency_overrides[verify_admin_access] = revoked
    response = await client.post(f"{ROOT}/query_tool/run", headers=ADMIN, content=b"select 1")
    assert response.status_code == 403
    assert len(seen) == 1


@pytest.mark.parametrize(
    "upstream",
    ["https://user:password@private.example", "file:///tmp/pgadmin", "http://pgadmin/bad"],
)
async def test_bad_configuration_is_refused_without_echoing_secrets(
    database_api, monkeypatch, upstream
):
    client, seen, _, _, _, _ = database_api
    monkeypatch.setenv("AGENT_PGADMIN_URL", upstream)
    response = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert response.status_code == 503
    assert upstream not in response.text
    assert seen == []


async def test_bare_path_and_root_variant_and_custom_api_prefix(database_api, monkeypatch):
    client, _, _, _, _, _ = database_api
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "http://localhost:3003")
    response = await client.get(ROOT, headers=ADMIN)
    assert response.headers["location"] == f"http://localhost:3003/api{ROOT}/"
    monkeypatch.setenv("AGENT_PGADMIN_PUBLIC_URL", f"https://api.example.org/control{ROOT}/")
    response = await client.get(ROOT, headers=ADMIN)
    assert response.headers["location"] == f"https://api.example.org/control{ROOT}/"


async def test_path_cannot_change_the_private_upstream_host(database_api):
    client, seen, _, _, _, _ = database_api
    await client.get(f"{ROOT}//other.example/path", headers=ADMIN)
    assert seen[0].url.host == "pgadmin"
