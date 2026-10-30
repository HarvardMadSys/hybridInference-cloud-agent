"""The local-account routes' boundary, without a database.

Gateway mode must not change: the new routes answer 404 there and the session
body keeps its exact shape. In local mode the routes refuse cross-site writes
without a cookie, accept only JSON, never echo a refused password or token,
and a Gateway-mode cookie or JWT signs nobody in. Gateway-only reading
surfaces answer a typed 503 without reaching the Gateway.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.support.owners import SESSIONS, use_session_secret

from cloud_agent.accounts import config
from cloud_agent.accounts.config import LOCAL_SESSION_COOKIE

pytestmark = pytest.mark.asyncio

_SECRET = "local-session-secret-for-tests-0123456789"
_SENTINEL = "Sentinel1Password" + "x" * 1100


class _NoGateway:
    """A Gateway client that fails the test if anything asks it anything."""

    def __getattr__(self, name):
        raise AssertionError(f"the Gateway was asked {name!r}")


@pytest_asyncio.fixture()
async def client(monkeypatch):
    use_session_secret(monkeypatch)
    monkeypatch.setenv(config.ENV_WEB_BASE_URL, "https://agents.example/agents")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token-for-tests-0123456789abcdef")
    from cloud_agent.app import create_app

    app = create_app()
    app.state.owners = _NoGateway()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test", follow_redirects=False
    ) as http:
        yield http


@pytest.fixture()
def local(monkeypatch):
    monkeypatch.setenv(config.ENV_AUTH_MODE, "local")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, _SECRET)


async def test_gateway_mode_offers_no_local_sign_up(client):
    response = await client.get("/v1/auth/config")
    assert response.status_code == 200
    assert response.json() == {"mode": "gateway", "signup_enabled": False}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/signup"),
        ("POST", "/v1/auth/verify-email"),
        ("POST", "/v1/auth/reset-password"),
        ("GET", "/v1/waitlist"),
        ("POST", "/v1/waitlist"),
        ("GET", "/v1/admin/accounts"),
        ("PATCH", "/v1/admin/accounts/01J0000000000000000000000A"),
    ],
)
async def test_gateway_mode_answers_404_for_every_local_route(client, method, path):
    response = await client.request(method, path, json={})
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["type"] == "local_auth_disabled"


async def test_local_mode_says_so_and_closes_sign_up_without_mail(client, local, monkeypatch):
    monkeypatch.delenv("AGENT_SMTP_HOST", raising=False)
    response = await client.get("/v1/auth/config")
    assert response.json() == {"mode": "local", "signup_enabled": False}
    monkeypatch.setenv("AGENT_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("AGENT_SMTP_FROM", "no-reply@example.org")
    response = await client.get("/v1/auth/config")
    assert response.json() == {"mode": "local", "signup_enabled": True}
    monkeypatch.setenv(config.ENV_SIGNUP_ENABLED, "false")
    assert (await client.get("/v1/auth/config")).json()["signup_enabled"] is False


@pytest.mark.parametrize("path", ["/v1/auth/login", "/v1/auth/signup", "/v1/auth/reset-password"])
async def test_a_cross_site_auth_post_is_refused_without_any_cookie(client, local, path):
    response = await client.post(
        path,
        json={"email": "a@example.org", "password": "x", "token": "t"},
        headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "cross_site_request"


async def test_auth_posts_accept_only_json(client, local):
    response = await client.post(
        "/v1/auth/login",
        content="email=a%40example.org&password=x",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["error"]["type"] == "unsupported_media_type"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/auth/signup", {"email": "a@example.org", "password": _SENTINEL}),
        ("/v1/auth/login", {"email": "a@example.org", "password": _SENTINEL}),
        ("/v1/auth/login", {"email": "a@example.org", "password": _SENTINEL, "role": "admin"}),
        ("/v1/auth/reset-password", {"token": _SENTINEL, "password": "Correct1Horse"}),
        ("/v1/auth/reset-password", {"token": "t", "password": _SENTINEL}),
        ("/v1/auth/verify-email", {"token": _SENTINEL}),
        ("/v1/auth/verify-email", {"token": 12345678901234567890}),
    ],
)
async def test_a_refused_body_never_repeats_a_password_or_token(client, local, caplog, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 422
    assert "Sentinel1Password" not in response.text
    assert "12345678901234567890" not in response.text
    assert "Sentinel1Password" not in caplog.text
    assert response.headers["cache-control"] == "no-store"
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])


async def test_gateway_sign_in_is_closed_in_local_mode(client, local):
    for path in ("/v1/session/login", "/v1/session/callback?code=c&state=s"):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json()["detail"]["error"]["type"] == "local_auth_disabled"


async def test_a_gateway_session_signs_nobody_in_locally(client, local):
    gateway_session = SESSIONS.sign_in("01J0000000000000000000000A")
    client.cookies.set(LOCAL_SESSION_COOKIE, gateway_session)
    assert (await client.get("/v1/session/me")).status_code == 401
    client.cookies.clear()
    response = await client.get(
        "/v1/session/me", headers={"authorization": f"Bearer {gateway_session}"}
    )
    assert response.status_code == 401
    response = await client.get(
        "/v1/agent/profile", headers={"authorization": f"Bearer {gateway_session}"}
    )
    assert response.status_code == 401


async def test_gateway_only_usage_views_are_unavailable_without_a_call(client, local):
    admin = {"authorization": "Bearer admin-token-for-tests-0123456789abcdef"}
    for path in ("/admin/agent/runs/j1/llm-requests", "/admin/agent/steps?job_id=j1"):
        response = await client.get(path, headers=admin)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["error"]["type"] == "gateway_feature_unavailable"
