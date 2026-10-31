"""The account session cookie: its name, its Secure flag, its lifetime and its expiry."""

from __future__ import annotations

import pytest
from starlette.responses import Response

from cloud_agent import settings
from cloud_agent.accounts.config import (
    ENV_SESSION_COOKIE_SECURE,
    LOCAL_SESSION_COOKIE,
    LOCAL_SESSION_COOKIE_INSECURE,
    local_session_ttl_s,
    session_cookie_is_secure,
)
from cloud_agent.accounts.session_cookie import (
    expire_local_session_cookies,
    local_session_from_cookies,
    set_local_session_cookie,
)


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    settings.reset_settings()
    monkeypatch.delenv(ENV_SESSION_COOKIE_SECURE, raising=False)
    yield
    settings.reset_settings()


def _cookies(response: Response) -> list[str]:
    return [value.decode() for name, value in response.raw_headers if name == b"set-cookie"]


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", " No "])
def test_only_a_deliberate_no_turns_secure_off(monkeypatch, value):
    monkeypatch.setenv(ENV_SESSION_COOKIE_SECURE, value)
    assert session_cookie_is_secure() is False


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "off", "disabled", "nope"])
def test_anything_else_keeps_secure_on(monkeypatch, value):
    monkeypatch.setenv(ENV_SESSION_COOKIE_SECURE, value)
    assert session_cookie_is_secure() is True


def test_a_new_session_lasts_exactly_its_configured_lifetime_as_a_host_cookie():
    response = Response()
    set_local_session_cookie(response, "session-token")
    (cookie,) = _cookies(response)
    assert cookie.startswith(f"{LOCAL_SESSION_COOKIE}=session-token;")
    assert f"Max-Age={local_session_ttl_s()}" in cookie
    assert local_session_ttl_s() == 30 * 24 * 60 * 60
    for attribute in ("HttpOnly", "Path=/", "SameSite=lax", "Secure"):
        assert attribute in cookie
    assert "Domain" not in cookie


def test_without_secure_the_cookie_keeps_its_plain_name(monkeypatch):
    monkeypatch.setenv(ENV_SESSION_COOKIE_SECURE, "false")
    response = Response()
    set_local_session_cookie(response, "session-token")
    (cookie,) = _cookies(response)
    assert cookie.startswith(f"{LOCAL_SESSION_COOKIE_INSECURE}=session-token;")
    assert "Secure" not in cookie


def test_sign_out_expires_both_names_over_https():
    response = Response()
    expire_local_session_cookies(response)
    expired = _cookies(response)
    assert {cookie.split("=", 1)[0] for cookie in expired} == {
        LOCAL_SESSION_COOKIE,
        LOCAL_SESSION_COOKIE_INSECURE,
    }
    assert all("Max-Age=0" in cookie and "Secure" in cookie for cookie in expired)


def test_sign_out_over_plain_http_expires_the_plain_name(monkeypatch):
    monkeypatch.setenv(ENV_SESSION_COOKIE_SECURE, "no")
    response = Response()
    expire_local_session_cookies(response)
    (cookie,) = _cookies(response)
    assert cookie.startswith(f"{LOCAL_SESSION_COOKIE_INSECURE}=")
    assert "Max-Age=0" in cookie


def test_the_plain_name_is_read_only_when_secure_is_off(monkeypatch):
    plain = {LOCAL_SESSION_COOKIE_INSECURE: "planted-over-http"}
    assert local_session_from_cookies(plain) is None
    assert local_session_from_cookies({LOCAL_SESSION_COOKIE: "real", **plain}) == "real"
    monkeypatch.setenv(ENV_SESSION_COOKIE_SECURE, "false")
    assert local_session_from_cookies(plain) == "planted-over-http"
    # The Host-bound name still wins where both are present.
    assert local_session_from_cookies({LOCAL_SESSION_COOKIE: "real", **plain}) == "real"
