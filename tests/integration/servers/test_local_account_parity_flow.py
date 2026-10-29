"""HybridInference account parity end to end: HTTP, real Postgres, mail and texts in memory.

The application is the real one with the local identity stand-ins the
lifespan installs. Mail goes to a recorder and text messages to a fake
provider; no message leaves the process and no Gateway is reachable. Each
test is one path a person or an administrator takes through the account
features: login names and first-run setup, several accounts in one browser,
changing the password or the email, deleting the account, phone codes, the
sign-up policy, administrator mail, and Gateway mode refusing all of it.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from tests.support.local_accounts import LOCAL_SECRET, PASSWORD, close_pool, make_account, open_pool

from cloud_agent.accounts import config
from cloud_agent.accounts.bootstrap_admin import create_account
from cloud_agent.accounts.directory import (
    LocalModeGateway,
    LocalModeGrantClient,
    LocalOwnerDirectory,
)
from cloud_agent.api import _owner_auth
from cloud_agent.application.account_broadcasts import send_due_broadcasts
from cloud_agent.application.owner_termination import enforce_ended_access
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ADMIN_TOKEN = "admin-token-for-tests-0123456789abcdef"
_ADMIN = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
_WEB = "https://agents.example/agents"
_COOKIE = "__Host-cloud_agent_local_session"


class _Mailer:
    def __init__(self) -> None:
        self.configured = True
        self.sent: list[Any] = []

    async def send(self, mail: Any) -> bool:
        self.sent.append(mail)
        return True

    def last(self, kind: str) -> Any:
        return next(mail for mail in reversed(self.sent) if mail.kind == kind)

    def token(self, kind: str) -> str:
        match = re.search(r"#token=([A-Za-z0-9_-]{43})$", self.last(kind).link)
        assert match, self.last(kind).link
        return match.group(1)


class _Texts:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_code(self, phone: str, code: str) -> bool:
        self.sent.append((phone, code))
        return True

    def code_for(self, phone: str) -> str:
        return next(code for number, code in reversed(self.sent) if number == phone)


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
    monkeypatch.setenv("ADMIN_TOKEN", _ADMIN_TOKEN)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    config._derived.cache_clear()
    from cloud_agent.app import create_app

    app = create_app()
    storage = Storage(pool)
    app.state.storage = storage
    app.state.gateway = LocalModeGateway(LocalOwnerDirectory(storage.local_accounts))
    app.state.grants = LocalModeGrantClient()
    app.state.account_mailer = _Mailer()
    app.state.sms_sender = _Texts()
    yield app


def _client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test", follow_redirects=False
    )


async def _login(http: AsyncClient, identifier: str, password: str = PASSWORD) -> Any:
    return await http.post("/v1/auth/login", json={"identifier": identifier, "password": password})


async def _version(app, account_id: str) -> int:
    record = await app.state.storage.local_accounts.get_account(account_id)
    assert record is not None
    return record.version


async def _admin(http: AsyncClient, app, account_id: str, action: str, **extra: Any) -> Any:
    return await http.patch(
        f"/v1/admin/accounts/{account_id}",
        json={"action": action, "expected_version": await _version(app, account_id), **extra},
        headers=_ADMIN,
    )


# ── Login names and first-run setup ──────────────────────────────────────


async def test_a_login_name_account_signs_in_without_any_email(env):
    account_id = await create_account(
        env.state.storage.local_accounts,
        email=None,
        login_name="ops",
        password=PASSWORD,
        admin=False,
    )
    async with _client(env) as http:
        wrong = await _login(http, "ops", "Wrong1Horse")
        assert wrong.status_code == 401
        signed_in = await _login(http, " OPS ")
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["identity"]["label"] == "ops"
        assert signed_in.json()["agent_access"]["allowed"] is True
        account = await http.get("/v1/auth/account")
        assert account.json()["login_name"] == "ops" and account.json()["email"] is None
        assert account_id == signed_in.json()["id"]


async def test_first_run_setup_creates_the_administrator_once(env):
    accounts = env.state.storage.local_accounts
    async with _client(env) as http:
        assert (await http.get("/v1/auth/options")).json()["setup_required"] is True
        code = await accounts.setup_code("AAAA-BBBB-CCCC")
        body = {"setup_code": "WRONG", "login_name": "root", "password": PASSWORD}
        assert (await http.post("/v1/auth/setup/admin", json=body)).status_code == 403
        body["setup_code"] = code.lower()
        created = await http.post("/v1/auth/setup/admin", json=body)
        assert created.status_code == 200, created.text
        assert created.json()["agent_access"]["permissions"] == ["agent.use", "agent.admin"]
        listed = await http.get("/v1/admin/accounts")
        assert listed.status_code == 200
        assert (await http.post("/v1/auth/setup/admin", json=body)).status_code == 409
        assert (await http.get("/v1/auth/options")).json()["setup_required"] is False


async def test_an_administrator_made_another_way_closes_setup(env):
    accounts = env.state.storage.local_accounts
    assert await accounts.setup_code("AAAA-BBBB-CCCC") is not None
    await create_account(accounts, email=None, login_name="ops", password=PASSWORD, admin=True)
    assert await accounts.setup_code("DDDD-EEEE-FFFF") is None
    assert not await accounts.setup_pending()


# ── Several accounts in one browser ──────────────────────────────────────


async def test_two_accounts_share_a_browser_and_a_tab_left_on_one_never_acts_as_the_other(env):
    storage = env.state.storage
    first = await make_account(storage, "first@example.edu", admission="approved")
    second = await make_account(storage, "second@example.edu", admission="approved")
    async with _client(env) as http:
        assert (await _login(http, "first@example.edu")).status_code == 200
        first_cookie = http.cookies[_COOKIE]
        assert (await _login(http, "second@example.edu")).status_code == 200
        assert http.cookies[_COOKIE] != first_cookie
        chooser = (await http.get("/v1/auth/accounts")).json()
        assert [(a["id"], a["active"]) for a in chooser["accounts"]] == [
            (first, False),
            (second, True),
        ]
        # The old cookie value no longer names the browser.
        stale = await http.get("/v1/session/me", cookies={_COOKIE: first_cookie})
        assert stale.status_code == 401
        pinned = await http.get("/v1/session/me", headers={"x-agent-account": first})
        assert pinned.status_code == 409
        assert pinned.json()["detail"]["error"]["type"] == "account_switched"

        cookie = f"{_COOKIE}={http.cookies[_COOKIE]}".encode()
        stream = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"cookie", cookie)],
                "app": env,
            }
        )
        _owner_auth.LOCAL_STREAM_RECHECK_S, kept = 0.0, _owner_auth.LOCAL_STREAM_RECHECK_S
        try:
            still_second = _owner_auth.owner_stream_guard(stream, second)
            assert await still_second()
            switched = await http.post("/v1/auth/switch", json={"account_id": first})
            assert switched.status_code == 200 and switched.json()["id"] == first
            # A stream opened for the second account ends; it never becomes the first's.
            assert not await still_second()
        finally:
            _owner_auth.LOCAL_STREAM_RECHECK_S = kept
        assert (await http.get("/v1/session/me")).json()["id"] == first

        removed = await http.delete(f"/v1/auth/accounts/{second}")
        assert [a["id"] for a in removed.json()["accounts"]] == [first]
        assert (await http.post("/v1/session/logout")).status_code == 200
        assert (await http.get("/v1/session/me")).status_code == 401


async def test_a_full_browser_refuses_a_sixth_account_but_not_a_member_signing_in_again(env):
    storage = env.state.storage
    emails = [f"member{index}@example.edu" for index in range(6)]
    for email in emails:
        await make_account(storage, email)
    async with _client(env) as http:
        for email in emails[:5]:
            assert (await _login(http, email)).status_code == 200
        sixth = await _login(http, emails[5])
        assert sixth.status_code == 409
        assert sixth.json()["detail"]["error"]["type"] == "browser_full"
        assert (await _login(http, emails[0])).status_code == 200
        assert (await http.post("/v1/auth/logout-all", json={})).status_code == 200
        assert (await http.get("/v1/auth/accounts")).json()["accounts"] == []


async def test_a_recognized_browser_signs_in_while_its_address_is_locked_by_others(env):
    await make_account(storage := env.state.storage, "owner@example.edu")
    assert storage
    async with _client(env) as owner, _client(env) as stranger:
        assert (await _login(owner, "owner@example.edu")).status_code == 200
        assert any(name.endswith("cloud_agent_device") for name in owner.cookies)
        await owner.post("/v1/session/logout")
        for _ in range(5):
            await _login(stranger, "owner@example.edu", "Wrong1Horse")
        assert (await _login(stranger, "owner@example.edu")).status_code == 429
        # Same address, but the owner's browser has its own window.
        assert (await _login(owner, "owner@example.edu")).status_code == 200


# ── The owner's own changes ──────────────────────────────────────────────


async def test_changing_the_password_keeps_this_browser_and_signs_out_every_other(env):
    await make_account(env.state.storage, "change@example.edu")
    async with _client(env) as here, _client(env) as there:
        assert (await _login(here, "change@example.edu")).status_code == 200
        assert (await _login(there, "change@example.edu")).status_code == 200
        wrong = await here.post(
            "/v1/auth/account/password",
            json={"old_password": "Wrong1Horse", "new_password": "Another1Horse"},
        )
        assert wrong.json()["detail"]["error"]["type"] == "wrong_password"
        same = await here.post(
            "/v1/auth/account/password",
            json={"old_password": PASSWORD, "new_password": PASSWORD},
        )
        assert same.json()["detail"]["error"]["type"] == "same_password"
        changed = await here.post(
            "/v1/auth/account/password",
            json={"old_password": PASSWORD, "new_password": "Another1Horse"},
        )
        assert changed.status_code == 200, changed.text
        assert (await here.get("/v1/session/me")).status_code == 200
        assert (await there.get("/v1/session/me")).status_code == 401
        assert env.state.account_mailer.last("password_changed").to == "change@example.edu"
        assert (await _login(there, "change@example.edu", "Another1Horse")).status_code == 200


async def test_a_new_email_takes_effect_only_from_its_link_and_the_old_one_is_told(env):
    await make_account(env.state.storage, "old@example.edu")
    mailer = env.state.account_mailer
    async with _client(env) as http:
        assert (await _login(http, "old@example.edu")).status_code == 200
        unproven = await http.post(
            "/v1/auth/account/email", json={"email": "new@example.edu", "password": "Wrong1Horse"}
        )
        assert unproven.json()["detail"]["error"]["type"] == "wrong_password"
        assert not any(mail.kind == "bind_email" for mail in mailer.sent)
        blocked = await http.post(
            "/v1/auth/account/email", json={"email": "x@mailinator.com", "password": PASSWORD}
        )
        assert blocked.json()["detail"]["error"]["type"] == "email_domain_blocked"
        asked = await http.post(
            "/v1/auth/account/email", json={"email": "New@Example.edu", "password": PASSWORD}
        )
        assert asked.json() == {"email": "New@example.edu", "email_verified": False}
        assert mailer.last("bind_email").to == "New@example.edu"
        assert (await http.get("/v1/auth/account")).json()["email"] == "old@example.edu"
        token = mailer.token("bind_email")
        assert (await http.post("/v1/auth/verify-email", json={"token": token})).status_code == 200
        assert (await http.get("/v1/auth/account")).json()["email"] == "New@example.edu"
        changed = mailer.last("email_changed")
        assert (changed.to, changed.details["new_email"]) == ("old@example.edu", "New@example.edu")
        assert (await _login(http, "old@example.edu")).status_code == 401
        replay = await http.post("/v1/auth/verify-email", json={"token": token})
        assert replay.status_code == 400


async def test_deleting_the_account_stops_everything_and_an_administrator_can_resume_it(env):
    storage = env.state.storage
    account_id = await make_account(storage, "leaving@example.edu", admission="approved")
    admin_id = await make_account(storage, "boss@example.edu", admission="approved", admin=True)
    async with _client(env) as http, _client(env) as boss:
        assert (await _login(boss, "boss@example.edu")).status_code == 200
        refused = await boss.post(
            "/v1/auth/account/delete", json={"confirm": "boss@example.edu", "password": PASSWORD}
        )
        assert refused.json()["detail"]["error"]["type"] == "self_delete_refused"
        assert admin_id
        assert (await _login(http, "leaving@example.edu")).status_code == 200
        mismatch = await http.post(
            "/v1/auth/account/delete", json={"confirm": "someone@example.edu", "password": PASSWORD}
        )
        assert mismatch.json()["detail"]["error"]["type"] == "confirmation_mismatch"
        deleted = await http.post(
            "/v1/auth/account/delete", json={"confirm": "LEAVING@example.edu", "password": PASSWORD}
        )
        assert deleted.status_code == 200, deleted.text
        assert (await http.get("/v1/session/me")).status_code == 401
        again = await _login(http, "leaving@example.edu")
        assert again.json()["detail"]["error"]["type"] == "account_deleted"
        assert env.state.account_mailer.last("account_deleted").to == "leaving@example.edu"
        await enforce_ended_access(storage, LocalModeGrantClient())
        resumed = await _admin(http, env, account_id, "resume")
        assert resumed.status_code == 200, resumed.text
        assert (resumed.json()["status"], resumed.json()["waitlist"]["status"]) == (
            "active",
            "revoked",
        )
        back = await _login(http, "leaving@example.edu")
        assert back.status_code == 200 and back.json()["agent_access"]["allowed"] is False


# ── Phone codes ──────────────────────────────────────────────────────────


async def test_phone_sign_up_sign_in_and_binding_through_a_fake_provider(env, pool, monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_SMS_ENABLED", "true")
    monkeypatch.setenv("AGENT_LOCAL_SMS_SEND_COOLDOWN_S", "60")
    texts = env.state.sms_sender
    phone = "+8613800138000"
    async with _client(env) as http:
        options = (await http.get("/v1/auth/options")).json()
        assert options["phone"]["enabled"] is True
        sent = await http.post("/v1/auth/phone/code", json={"phone": phone, "purpose": "signup"})
        assert sent.status_code == 200
        no_terms = await http.post(
            "/v1/auth/phone/signup", json={"phone": phone, "code": texts.code_for(phone)}
        )
        assert no_terms.json()["detail"]["error"]["type"] == "tos_required"
        created = await http.post(
            "/v1/auth/phone/signup",
            json={"phone": phone, "code": texts.code_for(phone), "accepted_tos": True},
        )
        assert created.status_code == 201 and created.json()["requires_approval"] is True
        # A number that has an account and one that has none answer alike.
        for number in (phone, "+8613800138111"):
            first = await http.post(
                "/v1/auth/phone/code", json={"phone": number, "purpose": "login"}
            )
            second = await http.post(
                "/v1/auth/phone/code", json={"phone": number, "purpose": "login"}
            )
            assert (first.status_code, second.status_code) in ((200, 429), (429, 429))
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_phone_codes SET created_at = NOW() - INTERVAL '2 minutes'"
            )
        assert (
            await http.post("/v1/auth/phone/code", json={"phone": phone, "purpose": "login"})
        ).status_code == 200
        assert texts.sent[-1][0] == phone
        signed_in = await http.post(
            "/v1/auth/phone/login", json={"phone": phone, "code": texts.code_for(phone)}
        )
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["identity"]["phone"] == phone
        replay = await http.post(
            "/v1/auth/phone/login", json={"phone": phone, "code": texts.code_for(phone)}
        )
        assert replay.json()["detail"]["error"]["type"] == "invalid_code"

    await make_account(env.state.storage, "binder@example.edu")
    async with _client(env) as http:
        assert (await _login(http, "binder@example.edu")).status_code == 200
        other = "+8613800138222"
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_local_phone_codes SET created_at = NOW() - INTERVAL '2 minutes'"
            )
        await http.post("/v1/auth/phone/code", json={"phone": other, "purpose": "bind"})
        bound = await http.post(
            "/v1/auth/account/phone", json={"phone": other, "code": texts.code_for(other)}
        )
        assert bound.status_code == 200 and bound.json()["phone"] == other
        # Another account's number: no code is sent, and none binds it.
        before = len(texts.sent)
        await http.post("/v1/auth/phone/code", json={"phone": phone, "purpose": "bind"})
        assert len(texts.sent) == before
        taken = await http.post("/v1/auth/account/phone", json={"phone": phone, "code": "123456"})
        assert taken.json()["detail"]["error"]["type"] == "invalid_code"


async def test_phone_sign_in_is_refused_until_switched_on(env):
    async with _client(env) as http:
        refused = await http.post(
            "/v1/auth/phone/code", json={"phone": "+8613800138000", "purpose": "signup"}
        )
        assert refused.status_code == 503
        assert refused.json()["detail"]["error"]["type"] == "phone_unavailable"


# ── Sign-up policy ───────────────────────────────────────────────────────


async def _sign_up(http: AsyncClient, email: str, **extra: Any) -> Any:
    return await http.post(
        "/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "accepted_tos": True, **extra},
    )


async def test_the_domain_policy_approves_verified_accounts_from_listed_domains(env, monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_APPROVAL", "domains")
    mailer = env.state.account_mailer
    async with _client(env) as http:
        listed = await http.post(
            "/v1/admin/accounts/signup-domains", json={"domain": "*.lab.edu"}, headers=_ADMIN
        )
        assert listed.status_code == 201, listed.text
        assert (await _sign_up(http, "a@cs.lab.edu", use_case="Thesis")).status_code == 200
        token = mailer.token("verify_email")
        await http.post("/v1/auth/verify-email", json={"token": token})
        approved = await _login(http, "a@cs.lab.edu")
        assert approved.json()["agent_access"]["allowed"] is True
        assert (await _sign_up(http, "b@elsewhere.edu", use_case="Hobby")).status_code == 200
        await http.post("/v1/auth/verify-email", json={"token": mailer.token("verify_email")})
        waiting = await _login(http, "b@elsewhere.edu")
        assert waiting.json()["agent_access"]["allowed"] is False
        assert waiting.json()["waitlist"]["status"] == "pending"
        notices = [mail for mail in mailer.sent if mail.kind == "new_application"]
        assert len(notices) == 0  # no administrator has an address, none is configured


async def test_sign_up_requires_the_terms_a_captcha_and_an_allowed_domain(env, monkeypatch):
    async with _client(env) as http:
        terms = await http.post(
            "/v1/auth/signup", json={"email": "t@example.edu", "password": PASSWORD}
        )
        assert terms.json()["detail"]["error"]["type"] == "tos_required"
        blocked = await _sign_up(http, "t@mailinator.com")
        assert blocked.json()["detail"]["error"]["type"] == "email_domain_blocked"
        monkeypatch.setenv("AGENT_TURNSTILE_SECRET_KEY", "synthetic-secret")

        async def refuse(token: str | None, ip: str | None) -> bool:
            return False

        monkeypatch.setattr("cloud_agent.api.auth.verify_captcha", refuse)
        captcha = await _sign_up(http, "t@example.edu", turnstile_token="x")
        assert captcha.json()["detail"]["error"]["type"] == "captcha_failed"


async def test_without_smtp_sign_up_works_once_verification_is_off(env, monkeypatch):
    env.state.account_mailer.configured = False
    async with _client(env) as http:
        assert (await http.get("/v1/auth/config")).json()["signup_enabled"] is False
        monkeypatch.setenv("AGENT_LOCAL_SIGNUP_REQUIRE_EMAIL_VERIFICATION", "false")
        options = (await http.get("/v1/auth/options")).json()
        assert options["signup_enabled"] is True and options["email_delivery"] is False
        assert (await _sign_up(http, "nosmtp@example.edu")).status_code == 200
        assert (await _login(http, "nosmtp@example.edu")).status_code == 200
        forgot = await http.post("/v1/auth/forgot-password", json={"email": "nosmtp@example.edu"})
        assert forgot.status_code == 503


async def test_a_listed_administrator_address_is_promoted_when_it_signs_in(env, monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_ADMIN_EMAILS", "Lead@Example.edu")
    await make_account(env.state.storage, "lead@example.edu")
    await make_account(env.state.storage, "unproven@example.edu", verified=False)
    async with _client(env) as http:
        signed_in = await _login(http, "lead@example.edu")
        assert signed_in.json()["agent_access"]["permissions"] == ["agent.use", "agent.admin"]


# ── Administration ───────────────────────────────────────────────────────


async def test_administrators_search_annotate_mail_outcomes_and_erase(env):
    storage = env.state.storage
    mailer = env.state.account_mailer
    applicant = await make_account(storage, "applicant@example.edu", admission="pending")
    async with _client(env) as http:
        found = await http.get("/v1/admin/accounts", params={"q": "APPLICANT"}, headers=_ADMIN)
        assert [a["id"] for a in found.json()["accounts"]] == [applicant]
        assert found.json()["counts"]["admission:pending"] == 1
        approved = await _admin(http, env, applicant, "approve")
        assert approved.status_code == 200
        assert mailer.last("approved").to == "applicant@example.edu"
        rejected_id = await make_account(storage, "rejected@example.edu", admission="pending")
        await _admin(http, env, rejected_id, "reject", reason="Not a lab member")
        assert mailer.last("rejected").details["reason"] == "Not a lab member"
        notes = await http.patch(
            f"/v1/admin/accounts/{applicant}/notes",
            json={
                "admin_note": "Known collaborator",
                "suspension_message": "Contact the lab office.",
                "expected_version": await _version(env, applicant),
            },
            headers=_ADMIN,
        )
        assert notes.json()["admin_note"] == "Known collaborator"
        await _admin(http, env, applicant, "suspend")
        suspended = await _login(http, "applicant@example.edu")
        assert suspended.json()["detail"]["error"]["message"] == "Contact the lab office."
        events = await http.get(f"/v1/admin/accounts/{applicant}/login-events", headers=_ADMIN)
        assert events.json()["events"][0]["failure_reason"] == "account_inactive"
        purged = await http.delete(
            "/v1/admin/accounts/login-events", params={"account_id": applicant}, headers=_ADMIN
        )
        assert purged.json()["deleted"] >= 1
        await _admin(http, env, applicant, "delete")
        await enforce_ended_access(storage, LocalModeGrantClient())
        mismatch = await http.post(
            f"/v1/admin/accounts/{applicant}/erase", json={"confirm": "wrong"}, headers=_ADMIN
        )
        assert mismatch.status_code == 400
        erased = await http.post(
            f"/v1/admin/accounts/{applicant}/erase", json={"confirm": applicant}, headers=_ADMIN
        )
        assert erased.status_code == 200, erased.text
        assert erased.json()["account"]["erased_at"] is not None
        assert (await _admin(http, env, applicant, "resume")).status_code == 409


async def test_administrator_mail_previews_tests_and_sends_to_its_audience(env):
    storage = env.state.storage
    mailer = env.state.account_mailer
    await make_account(storage, "reader@example.edu", admission="approved")
    await make_account(storage, "waiter@example.edu", admission="pending")
    admin_id = await make_account(storage, "sender@example.edu", admission="approved", admin=True)
    message = {
        "subject": "Upgrade tonight",
        "body_markdown": "Agent will be **down** for an hour. <script>x</script>",
        "target_statuses": ["active"],
        "target_admissions": ["approved"],
    }
    async with _client(env) as http:
        preview = await http.post("/v1/admin/account-mail/preview", json=message, headers=_ADMIN)
        assert preview.status_code == 200, preview.text
        assert preview.json()["recipient_count"] == 2
        assert "<strong>down</strong>" in preview.json()["html"]
        assert "<script>" not in preview.json()["html"]
        no_address = await http.post("/v1/admin/account-mail/test", json=message, headers=_ADMIN)
        assert no_address.json()["detail"]["error"]["type"] == "no_address"
        assert (await _login(http, "sender@example.edu")).status_code == 200
        tested = await http.post("/v1/admin/account-mail/test", json=message)
        assert tested.status_code == 200 and mailer.last("broadcast").to == "sender@example.edu"
        assert admin_id
        created = await http.post("/v1/admin/account-mail", json=message, headers=_ADMIN)
        assert created.status_code == 201 and created.json()["recipient_count"] == 2
        assert await send_due_broadcasts(storage.local_accounts, mailer) == 1
        delivered = {mail.to for mail in mailer.sent if mail.kind == "broadcast"}
        assert delivered == {"reader@example.edu", "sender@example.edu"}
        detail = await http.get(f"/v1/admin/account-mail/{created.json()['id']}", headers=_ADMIN)
        assert detail.json()["broadcast"]["status"] == "sent"
        cancel = await http.delete(f"/v1/admin/account-mail/{created.json()['id']}", headers=_ADMIN)
        assert cancel.status_code == 409


async def test_the_interface_language_follows_the_account(env):
    await make_account(env.state.storage, "lang@example.edu")
    async with _client(env) as http:
        assert (await _login(http, "lang@example.edu")).status_code == 200
        saved = await http.put("/v1/auth/account/locale", json={"locale": "zh-CN"})
        assert saved.json()["locale"] == "zh-CN"
        older = await http.put(
            "/v1/auth/account/locale",
            json={"locale": "en", "chosen_at": "2001-01-01T00:00:00Z"},
        )
        assert older.json()["locale"] == "zh-CN"
        assert (await http.get("/v1/session/me")).json()["identity"]["locale"] == "zh-CN"


# ── Gateway mode ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/auth/options"),
        ("POST", "/v1/auth/phone/code"),
        ("GET", "/v1/auth/accounts"),
        ("POST", "/v1/auth/switch"),
        ("POST", "/v1/auth/logout-all"),
        ("POST", "/v1/auth/account/password"),
        ("POST", "/v1/auth/account/delete"),
        ("PUT", "/v1/auth/account/locale"),
        ("POST", "/v1/auth/setup/admin"),
        ("GET", "/v1/admin/accounts/signup-domains"),
        ("GET", "/v1/admin/account-mail"),
    ],
)
async def test_gateway_mode_serves_none_of_the_local_account_routes(env, monkeypatch, method, path):
    monkeypatch.setenv(config.ENV_AUTH_MODE, "gateway")
    async with _client(env) as http:
        response = await http.request(method, path, json={}, headers={"origin": "https://test"})
        assert response.status_code == 404
