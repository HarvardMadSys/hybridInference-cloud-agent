"""Passwords, opaque credentials, configuration and mail for local accounts."""

from __future__ import annotations

import asyncio
import logging
import re

import pytest

from cloud_agent.accounts import config, passwords, tokens
from cloud_agent.accounts.mail import AccountMail, SmtpAccountMailer, render
from cloud_agent.application.accounts import SignedIn
from cloud_agent.ports.accounts import PasswordLogin

pytestmark = pytest.mark.asyncio

_SECRET = "local-session-secret-for-tests-0123456789"


@pytest.fixture(autouse=True)
def _local_secret(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, _SECRET)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    config._derived.cache_clear()


# ── The HybridInference password policy, unchanged ──────────────────────


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("Short1", "Password must be at least 8 characters long"),
        ("alllowercase1", "Password must contain at least one uppercase letter"),
        ("ALLUPPERCASE1", "Password must contain at least one lowercase letter"),
        ("NoDigitsHere", "Password must contain at least one number"),
        ("A1" + "a" * 1023, "Password must be at most 1024 characters long"),
    ],
)
async def test_the_policy_names_the_rule_a_password_breaks(password, message):
    assert passwords.validate_password_strength(password) == (False, message)


async def test_a_password_meeting_every_rule_passes():
    assert passwords.validate_password_strength("Correct1Horse") == (True, None)


async def test_hashes_are_argon2id_with_the_source_parameters():
    hashed = await passwords.hash_password("Correct1Horse")
    assert re.fullmatch(r"\$argon2id\$v=19\$m=65536,t=3,p=4\$[^$]+\$[^$]+", hashed)
    assert await passwords.verify_password("Correct1Horse", hashed)
    assert not await passwords.verify_password("Wrong1Horse", hashed)
    assert not passwords.needs_rehash(hashed)


async def test_an_unknown_account_is_checked_against_a_decoy_and_refused():
    assert await passwords.verify_password("Correct1Horse", None) is False
    assert await passwords.verify_password("Correct1Horse", "not-a-hash") is False


async def test_hashing_runs_off_the_event_loop(monkeypatch):
    loop_thread = []

    def record(password: str) -> str:
        import threading

        loop_thread.append(threading.current_thread().name)
        return "$argon2id$fake"

    monkeypatch.setattr(passwords, "_hash", record)
    await passwords.hash_password("Correct1Horse")
    assert loop_thread and loop_thread[0].startswith("agent-password")


async def test_a_full_queue_refuses_rather_than_holding_more_passwords(monkeypatch):
    monkeypatch.setattr(passwords, "_pending", passwords.MAX_PENDING_HASHES)
    with pytest.raises(passwords.HasherBusy):
        await passwords.hash_password("Correct1Horse")


# ── Opaque credentials ──────────────────────────────────────────────────


async def test_a_local_session_is_not_a_jwt_and_has_its_own_shape():
    token = tokens.new_session_token()
    assert tokens.looks_like_session_token(token)
    assert token.count(".") == 1
    assert not tokens.looks_like_session_token("eyJhbGciOiJIUzI1NiJ9.e30.sig")
    assert not tokens.looks_like_session_token("ajt." + "a" * 43)
    assert not tokens.looks_like_session_token(None)


async def test_stored_hashes_are_keyed_and_purpose_separated():
    token = tokens.new_email_token()
    assert tokens.looks_like_email_token(token)
    assert tokens.session_hash(token) != tokens.email_token_hash(token)
    assert len(tokens.email_token_hash(token)) == 32
    assert tokens.rate_limit_key("a", "x") != tokens.rate_limit_key("b", "x")


async def test_the_local_secret_is_required_long_and_not_the_gateway_session_key(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, "short")
    with pytest.raises(config.LocalAuthNotConfigured):
        tokens.session_hash("x")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, _SECRET)
    monkeypatch.setenv("AGENT_SESSION_SECRET", _SECRET)
    with pytest.raises(config.LocalAuthNotConfigured):
        config.check_local_configuration()


async def test_account_ids_are_ulids():
    ids = {tokens.new_account_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", value) for value in ids)


async def test_credentials_never_appear_in_a_repr():
    mail = AccountMail(kind="verify_email", to="p@example.org", link="https://x/#token=SECRET")
    assert "SECRET" not in repr(mail) and "p@example.org" not in repr(mail)
    signed_in = SignedIn(token="cals1.SECRET", account=None)  # type: ignore[arg-type]
    assert "SECRET" not in repr(signed_in)
    login = PasswordLogin(account=None, password_hash="$argon2id$SECRET")  # type: ignore[arg-type]
    assert "SECRET" not in repr(login)


# ── Configuration ───────────────────────────────────────────────────────


async def test_gateway_is_the_default_and_an_unknown_mode_stops(monkeypatch):
    monkeypatch.delenv(config.ENV_AUTH_MODE, raising=False)
    assert config.auth_mode() == "gateway"
    monkeypatch.setenv(config.ENV_AUTH_MODE, "LOCAL")
    assert config.auth_mode() == "local"
    monkeypatch.setenv(config.ENV_AUTH_MODE, "both")
    with pytest.raises(config.LocalAuthNotConfigured):
        config.auth_mode()


@pytest.mark.parametrize(
    "value",
    ["", "/relative", "https://x.example/?q=1", "https://x.example/#f", "https://u:p@x.example"],
)
async def test_mail_links_need_a_fixed_absolute_base(monkeypatch, value):
    monkeypatch.setenv(config.ENV_WEB_BASE_URL, value)
    with pytest.raises(config.LocalAuthNotConfigured):
        config.mail_web_base_url(allow_http=False)


async def test_plain_http_bases_are_for_insecure_development_only(monkeypatch):
    monkeypatch.setenv(config.ENV_WEB_BASE_URL, "http://localhost:3000/agents/")
    with pytest.raises(config.LocalAuthNotConfigured):
        config.mail_web_base_url(allow_http=False)
    assert config.mail_web_base_url(allow_http=True) == "http://localhost:3000/agents"


# ── Mail ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["verify_email", "reset_password", "account_exists"])
async def test_every_message_carries_its_link_and_escapes_it(kind):
    link = "https://agents.example/verify-email#token=abc<&>"
    subject, text, body = render(AccountMail(kind=kind, to="p@example.org", link=link))
    assert "Cloud Agent" in subject
    assert link in text
    assert "abc&lt;&amp;&gt;" in body and "abc<&>" not in body


async def test_an_unconfigured_relay_sends_nothing_and_logs_no_link(monkeypatch, caplog):
    for name in ("AGENT_SMTP_HOST", "AGENT_SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.DEBUG)
    mailer = SmtpAccountMailer()
    assert not mailer.configured
    sent = await mailer.send(AccountMail(kind="verify_email", to="p@example.org", link="L#SECRET"))
    assert sent is False
    assert "SECRET" not in caplog.text and "p@example.org" not in caplog.text


async def test_a_failed_delivery_logs_only_its_kind(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_SMTP_FROM", "Cloud Agent <no-reply@example.org>")
    mailer = SmtpAccountMailer()

    def refuse(_mail):
        raise ConnectionRefusedError("p@example.org L#SECRET")

    monkeypatch.setattr(mailer, "_deliver", refuse)
    caplog.set_level(logging.DEBUG)
    sent = await mailer.send(
        AccountMail(kind="reset_password", to="p@example.org", link="L#SECRET")
    )
    assert sent is False
    assert "SECRET" not in caplog.text and "p@example.org" not in caplog.text
    await asyncio.sleep(0)


async def test_an_unknown_smtp_security_mode_never_sends_in_plain_text(monkeypatch):
    import smtplib

    monkeypatch.setenv("AGENT_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("AGENT_SMTP_FROM", "no-reply@example.org")
    monkeypatch.setenv("AGENT_SMTP_USERNAME", "relay-user")
    monkeypatch.setenv("AGENT_SMTP_SECURITY", "startls")

    def no_connection(*_args, **_kwargs):
        raise AssertionError("a misconfigured relay was dialled")

    monkeypatch.setattr(smtplib, "SMTP", no_connection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", no_connection)
    mailer = SmtpAccountMailer()
    assert not mailer.configured
    assert (
        await mailer.send(AccountMail(kind="verify_email", to="p@example.org", link="L")) is False
    )
    with pytest.raises(ValueError):
        mailer._deliver(AccountMail(kind="verify_email", to="p@example.org", link="L"))
    for mode in ("starttls", "TLS", "none"):
        monkeypatch.setenv("AGENT_SMTP_SECURITY", mode)
        assert mailer.configured
