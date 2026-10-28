"""Local accounts end to end over HTTP and real Postgres, with mail captured in memory.

The application is the real one with the local identity stand-ins the
lifespan installs; no Gateway is reachable, and a call to it would fail. The
flow is the one a new user and an administrator take: sign up, verify, sign
in while pending, apply, be approved, use Agent, be suspended, recover the
password, be restored without access.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from tests.support.local_accounts import LOCAL_SECRET, close_pool, open_pool

from cloud_agent.accounts import config
from cloud_agent.accounts.directory import (
    LocalModeGateway,
    LocalModeGrantClient,
    LocalOwnerDirectory,
)
from cloud_agent.api import _owner_auth
from cloud_agent.api._local_auth import terminal_open_fence
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ADMIN = {"authorization": "Bearer admin-token-for-tests-0123456789abcdef"}
_WEB = "https://agents.example/agents"
_EMAIL = "Flow.Person@Example.org"
_PASSWORD = "Correct1Horse"


class _Mailer:
    configured = True

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, mail: Any) -> bool:
        self.sent.append(mail)
        return True

    def token(self, kind: str) -> str:
        mail = next(mail for mail in reversed(self.sent) if mail.kind == kind)
        page = "/verify-email" if kind == "verify_email" else "/reset-password"
        match = re.fullmatch(re.escape(_WEB + page) + r"#token=([A-Za-z0-9_-]{43})", mail.link)
        assert match, mail.link
        return match.group(1)


@pytest_asyncio.fixture
async def pool():
    created = await open_pool()
    try:
        yield created
    finally:
        await close_pool(created)


@pytest_asyncio.fixture
async def env(pool, monkeypatch):
    monkeypatch.setenv(config.ENV_AUTH_MODE, "local")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    monkeypatch.setenv(config.ENV_WEB_BASE_URL, _WEB)
    monkeypatch.setenv("ADMIN_TOKEN", _ADMIN["authorization"].removeprefix("Bearer "))
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    from cloud_agent.app import create_app

    app = create_app()
    storage = Storage(pool)
    app.state.storage = storage
    app.state.gateway = LocalModeGateway(LocalOwnerDirectory(storage.local_accounts))
    app.state.grants = LocalModeGrantClient()
    app.state.account_mailer = _Mailer()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test", follow_redirects=False
    ) as http:
        yield http, app


async def _sign_up_and_verify(http, app) -> None:
    response = await http.post("/v1/auth/signup", json={"email": _EMAIL, "password": _PASSWORD})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    token = app.state.account_mailer.token("verify_email")
    assert (await http.post("/v1/auth/verify-email", json={"token": token})).json() == {"ok": True}


async def _login(http, password: str = _PASSWORD) -> Any:
    return await http.post("/v1/auth/login", json={"email": _EMAIL.lower(), "password": password})


async def _version(http) -> int:
    return (await http.get("/v1/waitlist")).json()["version"]


async def _admin(http, account_id: str, action: str, version: int, **extra: Any) -> Any:
    return await http.patch(
        f"/v1/admin/accounts/{account_id}",
        json={"action": action, "expected_version": version, **extra},
        headers=_ADMIN,
    )


async def test_sign_up_answers_the_same_and_never_hands_a_stranger_the_password(env):
    http, app = env
    first = await http.post("/v1/auth/signup", json={"email": _EMAIL, "password": _PASSWORD})
    again = await http.post(
        "/v1/auth/signup", json={"email": _EMAIL.upper(), "password": "Stranger1Pass"}
    )
    assert first.json() == again.json() == {"ok": True}
    kinds = [mail.kind for mail in app.state.account_mailer.sent]
    assert kinds == ["verify_email", "account_exists"]
    # The second sign-up changed nothing: only the mailbox can set a password.
    await http.post(
        "/v1/auth/verify-email", json={"token": app.state.account_mailer.token("verify_email")}
    )
    assert (await _login(http, "Stranger1Pass")).status_code == 401
    assert (await _login(http)).status_code == 200


async def test_sign_in_refusals_say_nothing_about_who_exists(env):
    http, app = env
    unknown = await http.post(
        "/v1/auth/login", json={"email": "nobody@example.org", "password": _PASSWORD}
    )
    await http.post("/v1/auth/signup", json={"email": _EMAIL, "password": _PASSWORD})
    wrong = await _login(http, "Wrong1Horse")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["detail"]["error"]["type"] == "invalid_credentials"
    unverified = await _login(http)
    assert unverified.status_code == 403
    assert unverified.json()["detail"]["error"]["type"] == "email_not_verified"
    replayed = app.state.account_mailer.token("verify_email")
    assert (await http.post("/v1/auth/verify-email", json={"token": replayed})).status_code == 200
    again = await http.post("/v1/auth/verify-email", json={"token": replayed})
    assert again.status_code == 400
    assert again.json()["detail"]["error"]["type"] == "invalid_token"


async def test_the_whole_waiting_list_lifecycle(env):
    http, app = env
    await _sign_up_and_verify(http, app)

    login = await _login(http)
    assert login.status_code == 200, login.text
    body = login.json()
    cookie = login.headers["set-cookie"]
    assert cookie.startswith("__Host-cloud_agent_local_session=cals1.")
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert "cals1." not in login.text
    account_id = body["id"]
    assert body["auth_mode"] == "local" and body["email"] == "Flow.Person@example.org"
    assert body["agent_access"] == {"allowed": False, "permissions": []}
    assert body["waitlist"]["status"] == "not_applied"

    # Pending: identity works, owner routes do not.
    assert (await http.get("/v1/agent/profile")).status_code == 403
    applied = await http.post("/v1/waitlist", json={"purpose": "  build things  "})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "pending" and applied.json()["purpose"] == "build things"
    refused = await http.post("/v1/waitlist", json={"purpose": "x", "status": "approved"})
    assert refused.status_code == 422

    # Administration: stale versions are refused; a normal user is not an admin.
    listed = await http.get("/v1/admin/accounts?status=pending", headers=_ADMIN)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    entry = listed.json()["accounts"][0]
    assert entry["id"] == account_id and entry["email_verified"] is True
    version = entry["waitlist"]["version"]
    stale = await _admin(http, account_id, "approve", version - 1)
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["type"] == "version_conflict"
    assert (await http.get("/v1/admin/accounts")).status_code == 403
    approved = await _admin(http, account_id, "approve", version)
    assert approved.status_code == 200, approved.text
    assert approved.json()["waitlist"]["status"] == "approved"

    # Approved: Agent use, answered locally, including Profile's email.
    me = (await http.get("/v1/session/me")).json()
    assert me["agent_access"] == {"allowed": True, "permissions": ["agent.use"]}
    profile = await http.get("/v1/agent/profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["email"] == "Flow.Person@example.org"
    config_body = (await http.get("/v1/agent/config")).json()
    assert config_body["models"] == [] and config_body["model_connection_required"] is True

    granted = await _admin(http, account_id, "grant_admin", await _version(http))
    assert granted.status_code == 200 and granted.json()["is_admin"] is True
    assert (await http.get("/v1/admin/accounts")).status_code == 200

    # Suspended: the session ends at once and sign-in is refused.
    suspended = await _admin(http, account_id, "suspend", granted.json()["waitlist"]["version"])
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["is_admin"] is False
    assert suspended.json()["waitlist"]["status"] == "revoked"
    assert (await http.get("/v1/session/me")).status_code == 401
    blocked = await _login(http)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"]["type"] == "account_suspended"

    # Recovery works while suspended, and restoring grants nothing back.
    assert (await http.post("/v1/auth/forgot-password", json={"email": _EMAIL})).status_code == 200
    reset = app.state.account_mailer.token("reset_password")
    weak = await http.post("/v1/auth/reset-password", json={"token": reset, "password": "weak"})
    assert weak.status_code == 400
    assert weak.json()["detail"]["error"]["type"] == "weak_password"
    ok = await http.post(
        "/v1/auth/reset-password", json={"token": reset, "password": "Another1Horse"}
    )
    assert ok.status_code == 200
    restored = await _admin(
        http, account_id, "restore", suspended.json()["waitlist"]["version"] + 1
    )
    assert restored.status_code == 200, restored.text
    assert (await _login(http)).status_code == 401
    relogin = await _login(http, "Another1Horse")
    assert relogin.status_code == 200
    assert relogin.json()["agent_access"] == {"allowed": False, "permissions": []}
    assert relogin.json()["waitlist"]["status"] == "revoked"

    # Sign-out revokes the server-side session, so a kept copy stops working.
    kept = http.cookies.get("__Host-cloud_agent_local_session")
    assert (await http.post("/v1/session/logout")).json() == {"ok": True}
    http.cookies.set("__Host-cloud_agent_local_session", kept)
    assert (await http.get("/v1/session/me")).status_code == 401


async def test_repeated_wrong_passwords_are_throttled_across_requests(env):
    http, app = env
    await _sign_up_and_verify(http, app)
    statuses = [(await _login(http, "Wrong1Horse")).status_code for _ in range(6)]
    assert statuses[:5] == [401] * 5
    throttled = await _login(http)
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0
    assert statuses[5] == 429


async def test_a_stream_and_a_late_terminal_both_end_once_access_ends(env, monkeypatch):
    http, app = env
    await _sign_up_and_verify(http, app)
    login = await _login(http)
    account_id = login.json()["id"]
    await http.post("/v1/waitlist", json={"purpose": ""})
    assert (await _admin(http, account_id, "approve", await _version(http))).status_code == 200

    cookie = f"__Host-cloud_agent_local_session={http.cookies['__Host-cloud_agent_local_session']}"
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", cookie.encode())],
            "app": app,
        }
    )
    monkeypatch.setattr(_owner_auth, "LOCAL_STREAM_RECHECK_S", 0.0)
    still_admitted = _owner_auth.owner_stream_guard(request, account_id)
    assert await still_admitted()

    class Manager:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.fail = False

        async def delete_terminal(self, workspace: str, terminal_id: str) -> dict[str, Any]:
            if self.fail:
                raise ConnectionError("host away")
            self.deleted.append(terminal_id)
            return {}

    async def open_terminal(terminal_id: str) -> None:
        async with terminal_open_fence(storage, account_id, manager, "thread") as opened:
            opened["terminal"] = {"id": terminal_id}

    manager = Manager()
    storage = app.state.storage
    await open_terminal("t-ok")
    assert manager.deleted == []

    revoke = await _admin(http, account_id, "revoke", await _version(http))
    assert revoke.status_code == 200, revoke.text
    assert not await still_admitted()
    # The revocation's pass has finished (no work), so only the late open is left.
    assert await storage.local_accounts.open_terminations(limit=10) == []
    with pytest.raises(HTTPException) as refused:
        await open_terminal("t-late")
    assert refused.value.status_code == 403
    assert manager.deleted == ["t-late"]

    # A kill that fails still leaves the cleanup on record for the next pass.
    manager.fail = True
    with pytest.raises(HTTPException):
        await open_terminal("t-lost")
    assert [account for account, _ in await storage.local_accounts.open_terminations(limit=10)] == [
        account_id
    ]
    # So does an open whose outcome is unknown.
    await storage.local_accounts.record_termination_attempt(
        account_id=account_id,
        generation=(await storage.local_accounts.get_account(account_id)).version,
        complete=True,
        outcome="test",
    )
    with pytest.raises(TimeoutError):
        async with terminal_open_fence(storage, account_id, manager, "thread"):
            raise TimeoutError
    assert len(await storage.local_accounts.open_terminations(limit=10)) == 1
