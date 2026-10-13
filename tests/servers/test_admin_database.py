"""The pgAdmin proxy authorizes every request and isolates credentials."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from cloud_agent import settings
from cloud_agent.api import admin_database
from cloud_agent.api.deps import verify_admin_access
from cloud_agent.app import install_admin_no_store

ADMIN = {"Authorization": "Bearer test-database-admin"}
ROOT = "/admin/agent/pgadmin"
PUBLIC = f"https://agents.example.org/agents/api{ROOT}"


@pytest.fixture
async def database_api(monkeypatch):
    settings.reset_settings()
    monkeypatch.setenv("ADMIN_TOKEN", "test-database-admin")
    monkeypatch.setenv("AGENT_PGADMIN_URL", "http://pgadmin:5050")
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://agents.example.org/agents/")
    monkeypatch.delenv("AGENT_PGADMIN_PUBLIC_URL", raising=False)
    monkeypatch.delenv("AGENT_PGADMIN_BOOTSTRAP_DIR", raising=False)
    monkeypatch.delenv("AGENT_PGADMIN_BOOTSTRAP_URL", raising=False)
    monkeypatch.delenv("AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL", raising=False)
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
    settings.reset_settings()


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
    assert response.json() == {
        "enabled": True,
        "available": True,
        "url": f"{PUBLIC}/",
        "setup_required": False,
    }
    assert seen[0].url == "http://pgadmin:5050/misc/ping"
    assert "authorization" not in seen[0].headers
    assert response.headers["cache-control"] == "no-store"
    assert clients[0].is_closed


async def test_an_unconfigured_service_has_no_launch_link(database_api, monkeypatch):
    client, seen, _, _, _, _ = database_api
    monkeypatch.delenv("AGENT_PGADMIN_URL")
    response = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert response.json() == {
        "enabled": False,
        "available": False,
        "url": None,
        "setup_required": False,
    }
    assert (await client.get(f"{ROOT}/", headers=ADMIN)).status_code == 503
    assert seen == []


async def test_an_unavailable_service_has_no_launch_link_and_no_private_error_details(database_api):
    client, _, result, _, clients, _ = database_api
    result["error"] = httpx.ConnectError("private service diagnostics")
    status = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert status.json() == {
        "enabled": True,
        "available": False,
        "url": None,
        "setup_required": False,
    }
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
            "Cookie": (
                "cloud_agent_session=agent-secret; __Host-cloud_agent_session=agent-secret; "
                "pga4_session=pgadmin-session; other=secret"
            ),
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


SETUP = "/admin/agent/database-tools/setup"
CREDENTIALS = {"email": "first-admin@example.org", "password": "test-initial-password-42"}


@pytest.fixture
def setup_volume(database_api, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_URL", "http://pgadmin:5050")
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL", PUBLIC)
    database_api[2]["error"] = httpx.ConnectError("pgAdmin is waiting for setup")
    return tmp_path


async def test_setup_requires_current_admin_and_same_origin(database_api, setup_volume):
    client, seen, _, _, _, app = database_api
    assert (await client.post(SETUP, json=CREDENTIALS)).status_code == 401
    response = await client.post(
        SETUP, headers={**ADMIN, "Origin": "https://other.example.org"}, json=CREDENTIALS
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert seen == []
    assert list(setup_volume.iterdir()) == []

    async def revoked():
        raise HTTPException(status_code=403, detail="Admin permission revoked")

    app.dependency_overrides[verify_admin_access] = revoked
    assert (await client.post(SETUP, headers=ADMIN, json=CREDENTIALS)).status_code == 403
    assert list(setup_volume.iterdir()) == []


async def test_first_setup_is_private_once_only_and_audited(database_api, setup_volume):
    client, _, _, audit, _, _ = database_api
    assert (await client.get("/admin/agent/database-tools", headers=ADMIN)).json()["setup_required"]
    response = await client.post(
        SETUP, headers={**ADMIN, "Origin": "https://agents.example.org"}, json=CREDENTIALS
    )
    assert response.status_code == 202
    assert response.json() == {
        "enabled": True,
        "available": False,
        "url": None,
        "setup_required": False,
    }
    assert response.headers["cache-control"] == "no-store"
    for name, value in CREDENTIALS.items():
        path = setup_volume / name
        assert path.read_text() == value
        assert path.stat().st_mode & 0o777 == 0o640
        assert value not in response.text
    audit.record_admin_action.assert_awaited_once()
    assert audit.record_admin_action.call_args.kwargs["actor_id"] == "admin-token"
    assert CREDENTIALS["password"] not in str(audit.record_admin_action.call_args)
    assert not (await client.get("/admin/agent/database-tools", headers=ADMIN)).json()[
        "setup_required"
    ]

    repeat = await client.post(
        SETUP, headers=ADMIN, json={**CREDENTIALS, "password": "another-password-42"}
    )
    assert repeat.status_code == 409
    assert (setup_volume / "password").read_text() == CREDENTIALS["password"]


async def test_setup_cannot_replace_a_running_pgadmin_account(database_api, setup_volume):
    client, _, result, _, _, _ = database_api
    result["error"] = None
    assert (await client.post(SETUP, headers=ADMIN, json=CREDENTIALS)).status_code == 409
    assert list(setup_volume.iterdir()) == []


@pytest.mark.parametrize("configured", [False, True])
async def test_missing_setup_storage_is_sanitized(database_api, monkeypatch, tmp_path, configured):
    client, _, result, _, _, _ = database_api
    result["error"] = httpx.ConnectError("private diagnostics")
    monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_URL", "http://pgadmin:5050")
    if configured:
        monkeypatch.setenv("AGENT_PGADMIN_BOOTSTRAP_DIR", str(tmp_path / "missing"))
    response = await client.post(SETUP, headers=ADMIN, json=CREDENTIALS)
    assert response.status_code == 503
    assert str(tmp_path) not in response.text
    assert CREDENTIALS["password"] not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {**CREDENTIALS, "password": "tiny-secret"},
        {**CREDENTIALS, "password": "invalid-secret\npassword"},
        {**CREDENTIALS, "email": "unsafe'address@example.org"},
        {**CREDENTIALS, "email": "invalid-email"},
        [CREDENTIALS],
    ],
)
async def test_setup_validation_never_echoes_credentials(database_api, setup_volume, body):
    client, seen, _, _, _, _ = database_api
    response = await client.post(SETUP, headers=ADMIN, json=body)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "secret" not in response.text and CREDENTIALS["password"] not in response.text
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])
    assert list(setup_volume.iterdir()) == []
    assert seen == []


async def test_first_setup_enables_saved_settings_but_later_resets_remain_authoritative(
    database_api, setup_volume
):
    client, _, result, audit, _, app = database_api
    initial = {
        "pgadmin_url": "",
        "pgadmin_public_url": "",
        "web_base_url": "https://agents.example.org/agents",
    }
    settings.apply_settings(1, initial)
    repository = SimpleNamespace(update_settings=AsyncMock())
    repository.update_settings.return_value = (
        2,
        {**initial, "pgadmin_url": "http://pgadmin:5050", "pgadmin_public_url": PUBLIC},
        initial,
    )
    app.state.storage.settings = repository

    # A deployment can enable first setup after an existing settings row imported a blank URL.
    status = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert status.json()["setup_required"] is True
    assert (await client.post(SETUP, headers=ADMIN, json=CREDENTIALS)).status_code == 202
    repository.update_settings.assert_awaited_once_with(
        {"pgadmin_url": "http://pgadmin:5050", "pgadmin_public_url": PUBLIC}
    )
    assert CREDENTIALS["password"] not in str(audit.record_admin_action.call_args_list)
    result["error"] = None
    assert (await client.get("/admin/agent/database-tools", headers=ADMIN)).json()[
        "url"
    ] == f"{PUBLIC}/"

    # Resetting the runtime URL must not resurrect it from legacy environment values.
    settings.apply_settings(3, initial)
    disabled = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert disabled.json() == {
        "enabled": False,
        "available": False,
        "url": None,
        "setup_required": False,
    }
    assert (await client.get(f"{ROOT}/", headers=ADMIN)).status_code == 503
    assert (await client.post(SETUP, headers=ADMIN, json=CREDENTIALS)).status_code == 409
    assert repository.update_settings.await_count == 1


async def test_first_setup_does_not_replace_a_different_saved_pgadmin_service(
    database_api, setup_volume
):
    client, _, _, _, _, _ = database_api
    settings.apply_settings(
        1,
        {
            "pgadmin_url": "http://other-private-pgadmin:5050",
            "pgadmin_public_url": PUBLIC,
        },
    )
    status = await client.get("/admin/agent/database-tools", headers=ADMIN)
    assert status.json()["setup_required"] is False
    assert (await client.post(SETUP, headers=ADMIN, json=CREDENTIALS)).status_code == 409
    assert list(setup_volume.iterdir()) == []


async def test_proxied_pages_cannot_be_framed_elsewhere_or_sniffed(database_api):
    """pgAdmin keeps its own script policy; the proxy adds what cannot break it."""
    client, _, result, _, _, _ = database_api
    pgadmin_policy = "default-src ws: http: data: blob: 'unsafe-inline' 'unsafe-eval';"
    result["headers"] = [
        ("content-type", "text/html"),
        ("content-security-policy", pgadmin_policy),
        ("x-frame-options", "ALLOWALL"),
        ("x-content-type-options", "sniff-away"),
    ]
    response = await client.get(f"{ROOT}/browser/", headers=ADMIN)
    policies = response.headers.get_list("content-security-policy")
    assert pgadmin_policy in policies
    assert "frame-ancestors 'self'; object-src 'none'; base-uri 'self'" in policies
    assert response.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


async def test_the_first_request_of_a_pgadmin_session_is_audited_wherever_it_lands(
    database_api,
):
    """The launch link opens the root, but a browser can enter at any URL."""
    client, _, _, audit, _, _ = database_api
    session = {**ADMIN, "Cookie": "pga4_session=pgadmin-session"}

    await client.get(f"{ROOT}/browser/", headers=ADMIN)
    await client.post(f"{ROOT}/login", headers=ADMIN, content=b"email=a")
    await client.get(f"{ROOT}/browser/", headers=session)
    await client.get(f"{ROOT}/", headers=session)

    calls = [call.kwargs for call in audit.record_admin_action.await_args_list]
    assert [call["action"] for call in calls] == ["agent.database.pgadmin.open"] * 3
    assert [call["details"] for call in calls] == [
        {"pgadmin_session": "new"},
        {"pgadmin_session": "new"},
        {"pgadmin_session": "existing"},
    ]
    assert all(call["actor_id"] == "admin-token" for call in calls)
