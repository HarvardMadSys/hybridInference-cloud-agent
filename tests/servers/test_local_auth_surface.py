"""The account routes' boundary, without a database.

The routes refuse cross-site writes without a cookie, accept only JSON, never
echo a refused password or token, and a retired Gateway cookie or JWT signs
nobody in. The removed Gateway sign-in and usage routes are gone.
"""

from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.support.owners import SessionAccounts, use_session_secret

from cloud_agent.accounts import config
from cloud_agent.accounts.config import LOCAL_SESSION_COOKIE
from cloud_agent.api.deps import get_storage

pytestmark = pytest.mark.asyncio

_SECRET = "local-session-secret-for-tests-0123456789"
_SENTINEL = "Sentinel1Password" + "x" * 1100


class _NoDirectory:
    """A directory that fails the test if anything asks it anything."""

    def __getattr__(self, name):
        raise AssertionError(f"the directory was asked {name!r}")


@pytest_asyncio.fixture()
async def client(monkeypatch):
    use_session_secret(monkeypatch)
    monkeypatch.setenv(config.ENV_WEB_BASE_URL, "https://agents.example/agents")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token-for-tests-0123456789abcdef")
    from cloud_agent.app import create_app

    app = create_app()
    app.state.owners = _NoDirectory()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test", follow_redirects=False
    ) as http:
        yield http


async def test_sign_up_closes_without_mail(client, monkeypatch):
    monkeypatch.delenv("AGENT_SMTP_HOST", raising=False)
    response = await client.get("/v1/auth/config")
    assert response.json() == {"signup_enabled": False}
    monkeypatch.setenv("AGENT_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("AGENT_SMTP_FROM", "no-reply@example.org")
    response = await client.get("/v1/auth/config")
    assert response.json() == {"signup_enabled": True}
    monkeypatch.setenv(config.ENV_SIGNUP_ENABLED, "false")
    assert (await client.get("/v1/auth/config")).json()["signup_enabled"] is False


@pytest.mark.parametrize("path", ["/v1/auth/login", "/v1/auth/signup", "/v1/auth/reset-password"])
async def test_a_cross_site_auth_post_is_refused_without_any_cookie(client, path):
    response = await client.post(
        path,
        json={"email": "a@example.org", "password": "x", "token": "t"},
        headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "cross_site_request"


async def test_auth_posts_accept_only_json(client):
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
async def test_a_refused_body_never_repeats_a_password_or_token(client, caplog, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 422
    assert "Sentinel1Password" not in response.text
    assert "12345678901234567890" not in response.text
    assert "Sentinel1Password" not in caplog.text
    assert response.headers["cache-control"] == "no-store"
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])


async def test_gateway_sign_in_is_gone(client):
    for path in ("/v1/session/login", "/v1/session/callback?code=c&state=s"):
        assert (await client.get(path)).status_code == 404


async def test_a_retired_gateway_session_signs_nobody_in(client):
    client._transport.app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        local_accounts=SessionAccounts()
    )
    gateway_session = jwt.encode(
        {"sub": "01J0000000000000000000000A", "iss": "https://gateway.example"},
        _SECRET,
        algorithm="HS256",
    )
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


async def test_removed_gateway_usage_views_are_gone(client):
    admin = {"authorization": "Bearer admin-token-for-tests-0123456789abcdef"}
    for path in ("/admin/agent/runs/j1/llm-requests", "/admin/agent/steps?job_id=j1"):
        assert (await client.get(path, headers=admin)).status_code == 404
