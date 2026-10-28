"""Public account lifecycle over the migrated database and real application.

Only email delivery is replaced. Identity, passwords, sessions, admission,
administration, middleware and storage use their production implementations.
Gateway HTTP is forbidden, so independent accounts cannot pass by accidentally
consulting the old service. These tests do not claim provider or sandbox E2E.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.app import create_app
from cloud_agent.bootstrap.identity import attach_local_identity, bind_identity_source
from cloud_agent.gateway import GatewayClient
from cloud_agent.grants import GrantClient
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_EMAIL = "independent-owner@example.com"
_PASSWORD = "PrivateExample73!"
_ADMIN = "test-only-independent-account-admin"


@pytest_asyncio.fixture
async def acceptance(monkeypatch):
    database = os.environ.get("TEST_DB_NAME", "hybridinference_test_db")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    if worker != "master":
        database += f"_{worker}"
    if "_test_" not in database:
        pytest.fail("Account acceptance requires an explicitly named test database.")
    config = {
        "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": database,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    apply_migrations(config)
    pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    async with pool.acquire() as connection:
        await reset_agent_tables(connection)
    for key, value in {
        "AGENT_AUTH_MODE": "local",
        "AGENT_LOCAL_SESSION_SECRET": "independent-test-session-secret-over-32-bytes",
        "AGENT_WEB_BASE_URL": "https://agents.example",
        "AGENT_LOCAL_SIGNUP_ENABLED": "true",
        "ADMIN_TOKEN": _ADMIN,
    }.items():
        monkeypatch.setenv(key, value)

    async def forbidden_gateway(*args, **kwargs):
        pytest.fail("Independent account flow attempted Gateway HTTP")

    monkeypatch.setattr(GatewayClient, "_get", forbidden_gateway)
    monkeypatch.setattr(GrantClient, "_post", forbidden_gateway)
    mails = []

    async def record_mail(mail):
        mails.append(mail)
        return True

    app = create_app()
    app.state.storage = Storage(pool)
    app.state.account_mailer = SimpleNamespace(configured=True, send=record_mail)
    assert await bind_identity_source(app, pool) == "local"
    attach_local_identity(app, app.state.storage, local=True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://agents.example"
        ) as client:
            yield SimpleNamespace(client=client, mails=mails, pool=pool, app=app)
    finally:
        await pool.close()


def _mail_token(mail):
    parsed = urlsplit(mail.link)
    assert parsed.scheme == "https" and parsed.netloc == "agents.example"
    assert not parsed.query
    return parse_qs(parsed.fragment)["token"][0]


async def _verified_login(state):
    client = state.client
    credentials = {"email": _EMAIL, "password": _PASSWORD}
    response = await client.post("/v1/auth/signup", json=credentials)
    assert response.status_code == 200, response.text
    assert len(state.mails) == 1
    token = _mail_token(state.mails[-1])
    assert token not in response.text and _PASSWORD not in response.text
    unverified = await client.post("/v1/auth/login", json=credentials)
    assert unverified.status_code == 403
    assert unverified.json()["detail"]["error"]["type"] == "email_not_verified"
    verified = await client.post("/v1/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    replay = await client.post("/v1/auth/verify-email", json={"token": token})
    assert replay.status_code == 400
    signed_in = await client.post("/v1/auth/login", json=credentials)
    assert signed_in.status_code == 200, signed_in.text
    cookie = signed_in.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "cals1." not in signed_in.text
    return signed_in.json()


async def _admin_action(state, account_id, action, version, **extra):
    response = await state.client.patch(
        f"/v1/admin/accounts/{account_id}",
        headers={"Authorization": f"Bearer {_ADMIN}"},
        json={"action": action, "expected_version": version, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_signup_waitlist_review_revocation_and_restore_are_independent(acceptance):
    state = acceptance
    client = state.client
    user = await _verified_login(state)
    assert user["auth_mode"] == "local"
    assert user["waitlist"]["status"] == "not_applied"
    assert user["agent_access"]["allowed"] is False
    assert (await client.get("/v1/agent/jobs")).status_code == 403
    assert (await client.get("/v1/admin/accounts")).status_code == 403

    applied = await client.post("/v1/waitlist", json={"purpose": "Run my coding tasks"})
    assert applied.status_code == 200, applied.text
    pending = applied.json()
    assert pending["status"] == "pending"
    repeated = await client.post("/v1/waitlist", json={"purpose": "Must not replace first request"})
    assert repeated.status_code == 200
    assert repeated.json() == pending

    approved = await _admin_action(state, user["id"], "approve", pending["version"])
    stale = await client.patch(
        f"/v1/admin/accounts/{user['id']}",
        headers={"Authorization": f"Bearer {_ADMIN}"},
        json={"action": "revoke", "expected_version": pending["version"]},
    )
    assert stale.status_code == 409
    current = await client.get("/v1/session/me")
    assert current.json()["agent_access"]["allowed"] is True
    assert (await client.get("/v1/agent/jobs")).status_code == 200
    assert (await client.get("/v1/admin/accounts")).status_code == 403
    assert (await client.post("/v1/waitlist", json={"purpose": "again"})).status_code == 409

    revoked = await _admin_action(
        state, user["id"], "revoke", approved["waitlist"]["version"], reason="Access review"
    )
    assert (await client.get("/v1/agent/jobs")).status_code == 403
    current = await client.get("/v1/session/me")
    assert current.status_code == 200
    assert current.json()["waitlist"]["status"] == "revoked"
    assert current.json()["waitlist"]["review_reason"] == "Access review"

    suspended = await _admin_action(state, user["id"], "suspend", revoked["waitlist"]["version"])
    assert (await client.get("/v1/session/me")).status_code == 401
    await _admin_action(state, user["id"], "restore", suspended["waitlist"]["version"])
    signed_in = await client.post("/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["agent_access"]["allowed"] is False
    assert signed_in.json()["waitlist"]["status"] == "revoked"
    assert (await client.get("/v1/agent/jobs")).status_code == 403


async def test_password_reset_ends_every_browser_session_and_cannot_be_replayed(acceptance):
    state = acceptance
    client = state.client
    await _verified_login(state)
    first_cookies = dict(client.cookies)
    client.cookies.clear()
    second = await client.post("/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert second.status_code == 200
    second_cookies = dict(client.cookies)
    assert first_cookies != second_cookies
    reset = await client.post("/v1/auth/forgot-password", json={"email": _EMAIL})
    assert reset.status_code == 200
    token = _mail_token(state.mails[-1])
    new_password = "ChangedExample98!"
    changed = await client.post(
        "/v1/auth/reset-password", json={"token": token, "password": new_password}
    )
    assert changed.status_code == 200, changed.text
    assert token not in changed.text and new_password not in changed.text
    for cookies in (first_cookies, second_cookies):
        client.cookies.clear()
        client.cookies.update(cookies)
        assert (await client.get("/v1/session/me")).status_code == 401
    replay = await client.post(
        "/v1/auth/reset-password", json={"token": token, "password": "ThirdExample45!"}
    )
    assert replay.status_code == 400
    failed = await client.post("/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert failed.status_code == 401
    signed_in = await client.post(
        "/v1/auth/login", json={"email": _EMAIL, "password": new_password}
    )
    assert signed_in.status_code == 200, signed_in.text
    assert (await client.post("/v1/session/logout")).status_code == 200
    assert (await client.get("/v1/session/me")).status_code == 401


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("signup", {"email": "x", "password": {"sentinel": "SECRET_PASSWORD"}}),
        ("verify-email", {"token": {"sentinel": "SECRET_VERIFY_TOKEN"}}),
        ("reset-password", {"token": "SECRET_RESET_TOKEN", "password": []}),
    ],
)
async def test_invalid_account_bodies_never_echo_credentials(acceptance, path, body):
    response = await acceptance.client.post(f"/v1/auth/{path}", json=body)
    assert response.status_code == 422
    assert "SECRET_" not in response.text
    assert "no-store" in response.headers["cache-control"]


async def test_cross_site_signup_and_legacy_gateway_login_are_refused(acceptance):
    cross_site = await acceptance.client.post(
        "/v1/auth/signup",
        json={"email": _EMAIL, "password": _PASSWORD},
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://other.example"},
    )
    assert cross_site.status_code == 403
    assert not acceptance.mails
    legacy = await acceptance.client.get("/v1/session/login")
    assert legacy.status_code == 404
    assert "location" not in legacy.headers
