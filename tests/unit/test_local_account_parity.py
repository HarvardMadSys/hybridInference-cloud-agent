"""HybridInference account parity, without a database: rules, policy, codes and providers.

Everything here runs against in-memory stand-ins whose behaviour mirrors the
Postgres repository's contract; the database tier repeats the storage half
(``tests/integration/storage/test_local_account_parity_store.py``).
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from starlette.responses import Response

from cloud_agent.accounts import captcha, config, device_cookie, passwords, policy, tokens
from cloud_agent.accounts.mail import CREDENTIAL_KINDS, AccountMail, render
from cloud_agent.accounts.sms_tencent import TencentSms, signed_headers
from cloud_agent.application import accounts as flows_module
from cloud_agent.application.accounts import (
    AccountDeleted,
    AccountSuspended,
    EmailNotVerified,
    InvalidCredentials,
    LocalAccountFlows,
    identifier_key,
    parse_identifier,
    refuse_unusable_account,
)
from cloud_agent.application.phone_accounts import (
    InvalidPhone,
    InvalidPhoneCode,
    PhoneCooldown,
    PhoneFlows,
    PhoneSendFailed,
    normalize_phone,
)
from cloud_agent.domain.accounts import (
    AccountRecord,
    AccountTransitionRefused,
    SelfDeleteRefused,
    may_sign_in,
    permissions_for,
    plan_admin_action,
    plan_self_deletion,
)
from cloud_agent.ports.accounts import PasswordLogin

pytestmark = pytest.mark.asyncio

_SECRET = "local-session-secret-for-tests-0123456789"
_NOW = datetime(2026, 10, 29, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_secret(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, _SECRET)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    config._derived.cache_clear()


def _account(**changes: Any) -> AccountRecord:
    base = AccountRecord(
        id="01J0000000000000000000000A",
        email=None,
        email_verified=False,
        status="active",
        is_admin=False,
        version=1,
        created_at=_NOW,
        admission_status="approved",
        purpose="",
        review_reason=None,
        submitted_at=None,
        reviewed_at=None,
        identity_verified=True,
        login_name="operator",
    )
    return replace(base, **changes)


# ── Who may sign in and use Agent, whatever the identity ─────────────────


@pytest.mark.parametrize(
    ("changes", "signs_in"),
    [
        ({}, True),
        ({"login_name": None, "phone": "+8613800138000"}, True),
        ({"identity_verified": False}, False),
        ({"status": "suspended"}, False),
        ({"status": "deleted"}, False),
    ],
)
async def test_a_login_name_or_phone_account_is_judged_by_its_verified_identity(changes, signs_in):
    record = _account(**changes)
    assert may_sign_in(record) is signs_in
    assert permissions_for(record) == (["agent.use"] if signs_in else [])


async def test_an_unverified_identity_holds_nothing_even_with_a_verified_email_flag():
    # The flag is display only; the view's identity_verified is the authority.
    record = _account(email="p@example.edu", email_verified=True, identity_verified=False)
    assert not may_sign_in(record)
    assert permissions_for(record) == []
    with pytest.raises(AccountTransitionRefused):
        plan_admin_action(
            replace(record, admission_status="pending"),
            "approve",
            reason=None,
            actor_account_id=None,
        )


async def test_deleting_withdraws_access_and_resuming_never_brings_it_back():
    edit = plan_admin_action(_account(), "delete", reason=None, actor_account_id=None)
    assert (edit.account_status, edit.admission_status, edit.is_admin) == (
        "deleted",
        "revoked",
        False,
    )
    assert edit.ends_access
    deleted = _account(status="deleted", admission_status="revoked")
    resumed = plan_admin_action(deleted, "resume", reason=None, actor_account_id=None)
    assert (resumed.account_status, resumed.admission_status, resumed.ends_access) == (
        "active",
        "revoked",
        False,
    )
    for action in ("approve", "suspend", "restore", "delete"):
        with pytest.raises(AccountTransitionRefused):
            plan_admin_action(deleted, action, reason=None, actor_account_id=None)


async def test_an_erased_account_cannot_be_resumed():
    erased = _account(status="deleted", erased_at=_NOW)
    with pytest.raises(AccountTransitionRefused):
        plan_admin_action(erased, "resume", reason=None, actor_account_id=None)


async def test_an_administrator_cannot_delete_itself_either_way():
    admin = _account(is_admin=True)
    with pytest.raises(SelfDeleteRefused):
        plan_self_deletion(admin)
    with pytest.raises(AccountTransitionRefused) as refused:
        plan_admin_action(admin, "delete", reason=None, actor_account_id=admin.id)
    assert refused.value.code == "self_lockout"
    assert plan_self_deletion(_account()).ends_access


@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (_account(status="deleted"), AccountDeleted),
        (_account(status="suspended", suspension_message="Contact the lab."), AccountSuspended),
        (_account(identity_verified=False, status="suspended"), AccountSuspended),
        (_account(identity_verified=False), EmailNotVerified),
    ],
)
async def test_the_account_state_is_reported_before_an_address_it_cannot_use(record, refusal):
    with pytest.raises(refusal) as raised:
        refuse_unusable_account(record)
    if refusal is AccountSuspended and record.suspension_message:
        assert str(raised.value) == "Contact the lab."


# ── Identifiers and the keys their limits share ──────────────────────────


async def test_an_identifier_is_an_email_or_a_login_name():
    assert parse_identifier("  Person@Example.EDU ") == ("email", "person@example.edu")
    assert parse_identifier("Operator.1") == ("login_name", "operator.1")
    assert parse_identifier("ab") is None
    assert parse_identifier("has space") is None


async def test_every_spelling_shares_one_key_and_kinds_never_meet():
    assert identifier_key("Person@Example.edu") == identifier_key("person@example.EDU")
    assert identifier_key("Operator") == identifier_key(" operator ") == "raw:operator"
    assert not identifier_key("operator").count("@")
    assert identifier_key("a@b.edu") != identifier_key("raw:a@b.edu")


# ── The password pool ────────────────────────────────────────────────────


async def test_a_cancelled_call_keeps_its_slot_until_the_running_hash_ends(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(password: str) -> str:
        started.set()
        release.wait(5)
        return "$argon2id$done"

    monkeypatch.setattr(passwords, "_hash", slow)
    before = passwords._pending
    task = asyncio.ensure_future(passwords.hash_password("Correct1Horse"))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The hash still runs, holding its memory and its plaintext: it still counts.
    assert passwords._pending == before + 1
    release.set()
    for _ in range(100):
        if passwords._pending == before:
            break
        await asyncio.sleep(0.01)
    assert passwords._pending == before


async def test_a_saturated_pool_refuses_new_work_at_once(monkeypatch):
    release = threading.Event()

    def held(password: str) -> str:
        release.wait(5)
        return "$argon2id$held"

    monkeypatch.setattr(passwords, "_hash", held)
    monkeypatch.setattr(passwords, "MAX_PENDING_HASHES", 6)
    running = [asyncio.ensure_future(passwords.hash_password("Correct1Horse")) for _ in range(6)]
    await asyncio.sleep(0.05)
    try:
        with pytest.raises(passwords.HasherBusy):
            await passwords.hash_password("Correct1Horse")
    finally:
        release.set()
        await asyncio.gather(*running)


# ── Rehash: the losing compare-and-swap re-reads the winner's hash ───────


class _RehashAccounts:
    """The two calls a rehashing sign-in makes, as another sign-in won the race."""

    def __init__(self, winner_hash: str) -> None:
        self.winner_hash = winner_hash
        self.session_hash: str | None = None

    async def rehash_password(self, **_kwargs: Any) -> bool:
        return False

    async def stored_password_hash(self, account_id: str) -> str:
        return self.winner_hash


async def test_the_sign_in_that_loses_the_rehash_race_uses_the_winners_hash(monkeypatch):
    winner = await passwords.hash_password("Correct1Horse")
    accounts = _RehashAccounts(winner)
    flows = LocalAccountFlows(accounts, web_base_url=lambda: "", send=lambda mail: None)  # type: ignore[arg-type]
    monkeypatch.setattr(flows_module, "needs_rehash", lambda stored: True)
    login = PasswordLogin(account=_account(), password_hash="$argon2id$outdated")
    assert await flows._current_hash(login, "Correct1Horse", "$argon2id$outdated") == winner


async def test_a_password_changed_during_the_race_is_refused(monkeypatch):
    accounts = _RehashAccounts(await passwords.hash_password("Another1Horse"))
    flows = LocalAccountFlows(accounts, web_base_url=lambda: "", send=lambda mail: None)  # type: ignore[arg-type]
    monkeypatch.setattr(flows_module, "needs_rehash", lambda stored: True)
    login = PasswordLogin(account=_account(), password_hash="$argon2id$outdated")
    with pytest.raises(InvalidCredentials):
        await flows._current_hash(login, "Correct1Horse", "$argon2id$outdated")


# ── Sign-up policy ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("email", "blocked"),
    [
        ("a@example.com", True),
        ("a@mailinator.com", True),
        ("a@lab.test", True),
        ("a@localhost", True),
        ("a@harvard.edu", False),
        ("a@example.edu", False),
    ],
)
async def test_disposable_and_reserved_domains_are_refused(email, blocked):
    assert policy.is_email_domain_blocked(email) is blocked


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("Example.EDU", ("example.edu", False)),
        ("*.seas.harvard.edu", ("seas.harvard.edu", True)),
        ("lab.example.edu.", ("lab.example.edu", False)),
    ],
)
async def test_an_allowlist_entry_is_a_domain_or_a_subdomain_wildcard(raw, parsed):
    assert policy.parse_signup_domain(raw) == parsed


@pytest.mark.parametrize("raw", ["", "*", "*.edu*", "no_underscores.edu", "single", "a..b"])
async def test_a_malformed_allowlist_entry_is_refused(raw):
    with pytest.raises(ValueError):
        policy.parse_signup_domain(raw)


class _Domains:
    def __init__(self, empty: bool, allowed: set[str]) -> None:
        self.empty, self.allowed = empty, allowed

    async def signup_domain_policy(self, domain: str) -> tuple[bool, bool]:
        return self.empty, domain in self.allowed


@pytest.mark.parametrize(
    ("approval", "empty", "email", "approves"),
    [
        ("manual", True, "a@x.edu", False),
        ("domains", True, "a@x.edu", True),
        ("domains", True, None, True),
        ("domains", False, "a@x.edu", True),
        ("domains", False, "a@y.edu", False),
        ("domains", False, None, False),
    ],
)
async def test_approval_follows_hybridinference_under_the_domain_policy(
    monkeypatch, approval, empty, email, approves
):
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_APPROVAL", approval)
    domains = _Domains(empty, {"x.edu"})
    assert (
        await policy.approves_automatically(domains, email, proven=True)  # type: ignore[arg-type]
        is approves
    )


@pytest.mark.parametrize(("empty", "approves"), [(False, False), (True, True)])
async def test_a_listed_domain_approves_only_a_proven_address(monkeypatch, empty, approves):
    """With verification off an address is accepted unproven; a domain match then counts not."""
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_APPROVAL", "domains")
    domains = _Domains(empty, {"x.edu"})
    assert (
        await policy.approves_automatically(domains, "a@x.edu", proven=False)  # type: ignore[arg-type]
        is approves
    )


# ── Device cookies ───────────────────────────────────────────────────────


def _cookies_after(response: Response) -> dict[str, str]:
    header = response.headers["set-cookie"]
    name, rest = header.split("=", 1)
    return {name: rest.split(";", 1)[0]}


async def test_a_device_entry_names_its_account_and_nothing_else():
    response = Response()
    device_cookie.remember_device(response, ["person@example.edu"], None)
    header = response.headers["set-cookie"]
    # Path=/ so a browser reaching the API under /api or /agents/api sends it back.
    assert "Path=/;" in header and "HttpOnly" in header and "__Host-" in header
    cookies = _cookies_after(response)
    nonce = device_cookie.recognized_device(cookies, "person@example.edu")
    assert nonce is not None and len(nonce) == 32
    assert device_cookie.recognized_device(cookies, "other@example.edu") is None
    forged = {device_cookie.cookie_name(): "A" * 64}
    assert device_cookie.recognized_device(forged, "person@example.edu") is None


async def test_a_new_entry_rotates_its_own_and_keeps_at_most_five(monkeypatch):
    cookies: dict[str, str] = {}
    for index in range(7):
        response = Response()
        device_cookie.remember_device(response, [f"user{index}@example.edu"], cookies)
        cookies = _cookies_after(response)
    kept = [
        index
        for index in range(7)
        if device_cookie.recognized_device(cookies, f"user{index}@example.edu")
    ]
    assert kept == [2, 3, 4, 5, 6]
    first = device_cookie.recognized_device(cookies, "user6@example.edu")
    response = Response()
    device_cookie.remember_device(response, ["user6@example.edu"], cookies)
    assert device_cookie.recognized_device(_cookies_after(response), "user6@example.edu") != first


async def test_an_expired_entry_or_another_secret_is_not_recognized(monkeypatch):
    response = Response()
    device_cookie.remember_device(response, ["person@example.edu"], None)
    cookies = _cookies_after(response)
    monkeypatch.setattr(device_cookie, "_now", lambda: 2**31)
    assert device_cookie.recognized_device(cookies, "person@example.edu") is None
    monkeypatch.undo()
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, "another-local-secret-for-tests-987654321")
    config._derived.cache_clear()
    assert device_cookie.recognized_device(cookies, "person@example.edu") is None


# ── Phone numbers, codes and the provider ────────────────────────────────


async def test_numbers_are_canonical_and_within_the_deployments_prefixes(monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_SMS_DEFAULT_COUNTRY_CODE", "86")
    assert normalize_phone("138 0013 8000") == "+8613800138000"
    assert normalize_phone("+1 (202) 555-0123") == "+12025550123"
    monkeypatch.setenv("AGENT_LOCAL_SMS_ALLOWED_PREFIXES", "+86")
    with pytest.raises(InvalidPhone):
        normalize_phone("+12025550123")
    with pytest.raises(InvalidPhone):
        normalize_phone("12")


async def test_a_code_is_bound_to_its_number_purpose_and_account():
    base = tokens.phone_code_hash("+8613800138000", "login", "A", "123456")
    assert base != tokens.phone_code_hash("+8613800138001", "login", "A", "123456")
    assert base != tokens.phone_code_hash("+8613800138000", "bind", "A", "123456")
    assert base != tokens.phone_code_hash("+8613800138000", "login", "B", "123456")
    assert base != tokens.phone_code_hash("+8613800138000", "login", "A", "123457")
    assert tokens.phone_hash("+8613800138000") != base


class _PhoneAccounts:
    """The phone-code half of the repository, in memory, with the same rules."""

    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners
        self.codes: dict[tuple[bytes, str, str], dict[str, Any]] = {}

    async def identifier_owner(self, kind: str, normalized: str) -> str | None:
        return self.owners.get(normalized)

    async def reserve_phone_code(self, **fields: Any) -> int | None:
        key = (fields["phone_hash"], fields["purpose"], fields["account_id"] or "")
        self.codes[key] = {**fields, "active": False, "attempts": 0, "consumed": False}
        return 0

    async def activate_phone_code(self, challenge_id: str) -> bool:
        for code in self.codes.values():
            if code["challenge_id"] == challenge_id:
                code["active"] = True
                return True
        return False

    async def invalidate_phone_code(self, challenge_id: str) -> None:
        self.codes = {k: v for k, v in self.codes.items() if v["challenge_id"] != challenge_id}

    async def consume_phone_code(self, **fields: Any) -> bool:
        code = self.codes.get((fields["phone_hash"], fields["purpose"], fields["account_id"] or ""))
        if not code or not code["active"] or code["consumed"]:
            return False
        if code["attempts"] >= fields["max_attempts"]:
            return False
        code["attempts"] += 1
        matched = code["code_hash"] == fields["code_hash"]
        code["consumed"] = matched
        return matched


class _Sender:
    configured = True

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.sent: list[tuple[str, str]] = []

    def send_code(self, phone: str, code: str) -> bool:
        self.sent.append((phone, code))
        return self.accept


def _phone_flows(accounts: _PhoneAccounts, sender: _Sender) -> PhoneFlows:
    flows = LocalAccountFlows(accounts, web_base_url=lambda: "", send=lambda mail: None)  # type: ignore[arg-type]
    return PhoneFlows(flows, sender)  # type: ignore[arg-type]


@pytest.fixture()
def _sms_on(monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_SMS_ENABLED", "true")


@pytest.mark.usefixtures("_sms_on")
@pytest.mark.parametrize(
    ("purpose", "account", "sent"),
    [
        ("login", None, True),
        ("signup", None, False),
        ("bind", "A", True),
        ("bind", "B", False),
        ("bind", "C", False),
        ("delete_account", "A", True),
        ("delete_account", "B", False),
    ],
)
async def test_a_code_is_sent_only_where_it_can_be_used(purpose, account, sent):
    accounts = _PhoneAccounts({"+8613800138000": "A"})
    sender = _Sender()
    await _phone_flows(accounts, sender).send_code("+8613800138000", purpose, account)
    assert bool(sender.sent) is sent
    # A dropped code leaves nothing that could be spent.
    assert bool(accounts.codes) is sent


@pytest.mark.usefixtures("_sms_on")
async def test_a_sent_code_verifies_once_and_a_failed_send_leaves_none(monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_SMS_MAX_ATTEMPTS", "2")
    accounts = _PhoneAccounts({"+8613800138000": "A"})
    sender = _Sender()
    flows = _phone_flows(accounts, sender)
    await flows.send_code("+8613800138000", "login", None)
    code = sender.sent[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(InvalidPhoneCode):
        await flows.verify("+8613800138000", "login", "A", wrong)
    await flows.verify("+8613800138000", "login", "A", code)
    with pytest.raises(InvalidPhoneCode):
        await flows.verify("+8613800138000", "login", "A", code)
    with pytest.raises(PhoneSendFailed):
        await _phone_flows(accounts, _Sender(accept=False)).send_code(
            "+8613800138000", "login", None
        )
    assert not any(c["active"] for c in accounts.codes.values())


async def test_phone_sign_in_is_off_until_switched_on_and_configured():
    flows = _phone_flows(_PhoneAccounts({}), _Sender())
    assert not flows.enabled


@pytest.mark.usefixtures("_sms_on")
async def test_a_cooling_number_is_told_how_long_to_wait():
    class Cooling(_PhoneAccounts):
        async def reserve_phone_code(self, **fields: Any) -> int | None:
            return 42

    with pytest.raises(PhoneCooldown) as cooling:
        await _phone_flows(Cooling({}), _Sender()).send_code("+8613800138000", "signup", None)
    assert cooling.value.retry_after == 42


def _tencent_settings(monkeypatch) -> None:
    for name, value in {
        "AGENT_TENCENT_CLOUD_SECRET_ID": "synthetic-id",
        "AGENT_TENCENT_CLOUD_SECRET_KEY": "synthetic-key",
        "AGENT_TENCENT_SMS_SDK_APP_ID": "1400000000",
        "AGENT_TENCENT_SMS_SIGN_NAME": "Synthetic",
        "AGENT_TENCENT_SMS_TEMPLATE_ID": "100",
    }.items():
        monkeypatch.setenv(name, value)


async def test_tencent_signs_exactly_the_body_it_sends(monkeypatch):
    _tencent_settings(monkeypatch)
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        number = json.loads(request.content)["PhoneNumberSet"][0]
        return httpx.Response(
            200, json={"Response": {"SendStatusSet": [{"Code": "Ok", "PhoneNumber": number}]}}
        )

    sender = TencentSms(transport=httpx.MockTransport(answer))
    assert sender.configured
    assert sender.send_code("+8613800138000", "123456")
    request = seen[0]
    payload = json.loads(request.content)
    assert payload["TemplateParamSet"] == ["123456", "5"]
    timestamp = int(request.headers["X-TC-Timestamp"])
    expected = signed_headers(
        service="sms",
        action="SendSms",
        version="2021-01-11",
        region="ap-guangzhou",
        body=request.content,
        secret_id="synthetic-id",
        secret_key="synthetic-key",
        timestamp=timestamp,
    )
    assert request.headers["Authorization"] == expected["Authorization"]
    assert "synthetic-key" not in request.headers["Authorization"]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"Response": {"Error": {"Code": "LimitExceeded"}}}),
        httpx.Response(200, json={"Response": {"SendStatusSet": [{"Code": "Failed"}]}}),
        httpx.Response(500, text="secret-key leaked in an error page"),
    ],
)
async def test_tencent_refusals_are_false_and_log_only_a_bounded_code(monkeypatch, caplog, answer):
    _tencent_settings(monkeypatch)
    sender = TencentSms(transport=httpx.MockTransport(lambda request: answer))
    assert not sender.send_code("+8613800138000", "123456")
    assert "123456" not in caplog.text and "secret-key" not in caplog.text


async def test_tencent_takes_only_mainland_numbers_and_six_digits(monkeypatch):
    _tencent_settings(monkeypatch)
    sender = TencentSms(transport=httpx.MockTransport(lambda request: pytest.fail("sent")))
    assert not sender.send_code("+12025550123", "123456")
    assert not sender.send_code("+8613800138000", "12345")


# ── Captcha ──────────────────────────────────────────────────────────────


async def test_without_a_secret_captcha_is_not_checked(monkeypatch):
    monkeypatch.delenv("AGENT_TURNSTILE_SECRET_KEY", raising=False)
    assert await captcha.verify_captcha(None, None)


@pytest.mark.parametrize(
    ("token", "answer", "valid"),
    [
        (None, None, False),
        ("token", httpx.Response(200, json={"success": True}), True),
        ("token", httpx.Response(200, json={"success": False}), False),
        ("token", httpx.Response(502), False),
        ("token", httpx.Response(200, text="not json"), False),
    ],
)
async def test_with_a_secret_captcha_fails_closed(monkeypatch, token, answer, valid):
    monkeypatch.setenv("AGENT_TURNSTILE_SECRET_KEY", "synthetic-turnstile-secret")
    sent: list[bytes] = []

    def reply(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        assert answer is not None
        return answer

    result = await captcha.verify_captcha(
        token, "203.0.113.5", transport=httpx.MockTransport(reply)
    )
    assert result is valid
    if sent:
        assert b"remoteip=203.0.113.5" in sent[0]


async def test_an_unreachable_captcha_refuses(monkeypatch):
    monkeypatch.setenv("AGENT_TURNSTILE_SECRET_KEY", "synthetic-turnstile-secret")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert not await captcha.verify_captcha("token", None, transport=httpx.MockTransport(fail))


# ── Mail ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "details", "expected"),
    [
        ("approved", {}, "approved"),
        ("rejected", {"reason": "Not a lab member"}, "Reason: Not a lab member"),
        (
            "new_application",
            {"account": "p@example.edu", "purpose": "Thesis"},
            "Use case: Thesis",
        ),
        ("password_changed", {"first_password": "true"}, "was set"),
        ("email_changed", {"new_email": "n@example.edu"}, "n@example.edu"),
        ("account_deleted", {}, "deleted"),
        ("bind_email", {}, "Confirm your new"),
    ],
)
async def test_every_notice_renders_and_hides_its_details_from_repr(kind, details, expected):
    mail = AccountMail(
        kind=kind, to="p@example.edu", link="https://x/#token=SECRET", details=details
    )
    subject, text, html = render(mail)
    assert expected in subject + text
    assert "<p>" in html
    assert "SECRET" not in repr(mail) and "p@example.edu" not in repr(mail)
    assert ("token=SECRET" in text) is (kind in CREDENTIAL_KINDS | {"approved", "new_application"})
