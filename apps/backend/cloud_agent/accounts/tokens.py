"""Opaque local credentials: session cookies, emailed tokens and account ids.

A local session is a random value the database knows only by its keyed hash.
It is not a JWT and carries no claims, so it cannot be mistaken for a
Gateway-mode session (an HS256 JWT) or a worker token (``ajt.``), and nothing
in it is an authorization cache: every request reads the account afresh.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from cloud_agent.accounts.config import derived_key

#: The local session's recognisable shape: a version prefix and 256 random bits.
SESSION_PREFIX = "cals1."
_SESSION_SHAPE = re.compile(r"cals1\.[A-Za-z0-9_-]{43}")

#: A browser's cookie value names the browser session holding its signed-in
#: accounts. It has the local session's shape: the cookie keeps its name, its
#: attributes and its value's form, and a value is looked up as a browser
#: first and as a single account's session (a bearer, or a cookie from before
#: browsers) second. The two are hashed under different keys.
BROWSER_PREFIX = SESSION_PREFIX

#: An emailed token: 256 random bits, URL-safe, no prefix.
_EMAIL_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_session_token() -> str:
    """Return a fresh local session value."""
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def looks_like_session_token(value: str | None) -> bool:
    """Whether a presented value has the local session's shape."""
    return bool(value) and _SESSION_SHAPE.fullmatch(value or "") is not None


def new_browser_token() -> str:
    """Return a fresh browser cookie value."""
    return BROWSER_PREFIX + secrets.token_urlsafe(32)


def looks_like_browser_token(value: str | None) -> bool:
    """Whether a presented value may name a browser: the local session's shape."""
    return looks_like_session_token(value)


def browser_hash(token: str) -> bytes:
    """Hash a browser cookie value for lookup."""
    return keyed_hash("browser", token)


def phone_hash(phone: str) -> bytes:
    """Hash a canonical phone number: codes are keyed by it, never by the number."""
    return keyed_hash("phone", phone)


def phone_code_hash(phone: str, purpose: str, account_id: str | None, code: str) -> bytes:
    """Bind a six-digit code to its number, purpose and account, under a key of its own.

    Domain separation keeps a code for one purpose or account from verifying
    another, and the key keeps a leaked table from enumerating the codes.
    """
    return keyed_hash(
        "phone-code", "\x00".join(("phone-otp-v1", phone, purpose, account_id or "", code))
    )


def new_email_token() -> str:
    """Return a fresh single-use token for a verification or reset link."""
    return secrets.token_urlsafe(32)


def looks_like_email_token(value: str | None) -> bool:
    """Whether a presented value has an emailed token's shape."""
    return bool(value) and _EMAIL_TOKEN_SHAPE.fullmatch(value or "") is not None


def keyed_hash(purpose: str, value: str) -> bytes:
    """The 32-byte keyed hash the database stores in place of ``value``."""
    return hmac.new(derived_key(purpose), value.encode(), hashlib.sha256).digest()


def session_hash(token: str) -> bytes:
    """Hash a session value for lookup."""
    return keyed_hash("session", token)


def email_token_hash(token: str) -> bytes:
    """Hash an emailed token for lookup."""
    return keyed_hash("email-token", token)


def rate_limit_key(bucket: str, value: str) -> bytes:
    """Hash an abuse-counter key; raw emails and addresses are never stored."""
    return keyed_hash("rate-limit", f"{bucket}\x00{value}")


def new_account_id() -> str:
    """Mint a ULID: 48 bits of milliseconds, then 80 random bits, Crockford base32."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_session_id() -> str:
    """A non-secret handle for one session row."""
    return secrets.token_hex(16)


__all__ = [
    "BROWSER_PREFIX",
    "SESSION_PREFIX",
    "browser_hash",
    "email_token_hash",
    "keyed_hash",
    "looks_like_browser_token",
    "looks_like_email_token",
    "looks_like_session_token",
    "new_account_id",
    "new_browser_token",
    "new_email_token",
    "new_session_id",
    "new_session_token",
    "phone_code_hash",
    "phone_hash",
    "rate_limit_key",
    "session_hash",
]
