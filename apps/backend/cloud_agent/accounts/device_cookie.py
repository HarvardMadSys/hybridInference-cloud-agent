"""Device cookies: recognize a browser that has signed in to an account before.

Adapted from HybridInference ``utils/device_cookie.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS), itself OWASP's "Slow down online guessing attacks with device
cookies". After a successful password sign-in the browser is given a signed
entry naming the account, and a later sign-in (or owner confirmation) to that
account presenting it is counted in a rate-limit window of its own, apart from
every other client. Without it, whoever knows an identifier can keep the
account locked out wherever clients share an address.

The cookie is stateless. It holds up to :data:`MAX_ENTRIES` entries, newest
first, one per account signed in on the browser. An entry is a random nonce,
an expiry and, for each of the account's sign-in identifiers (its email and
its login name, whichever exist, keyed as the sign-in limiter keys them), the
HMAC-SHA256 of nonce, expiry and identifier under a key derived from
``AGENT_LOCAL_SESSION_SECRET`` for this use alone (HybridInference derives it
from its JWT secret). An entry the backend did not issue, an expired one or
one for another account is passed over silently, and that attempt is limited
like any other. The nonce keys the entry's window.

An entry carries no authority beyond that window, since the password is
checked exactly as always, so nothing revokes one: signing out keeps it, and
it expires on its own (:data:`LIFETIME_SECONDS`). Changing the local secret
invalidates every entry.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent.accounts.config import LocalAuthNotConfigured, derived_key
from cloud_agent.identity import session_cookie_is_secure

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from starlette.responses import Response

#: Entries one cookie keeps: as many accounts as one browser may hold.
MAX_ENTRIES = 5
#: How long an entry, and the cookie, last after the sign-in that issued them.
LIFETIME_SECONDS = 180 * 24 * 60 * 60
#: ``Path=/``: the browser reaches the API under a prefix this service does not
#: know (``/api``, ``/agents/api``), so a narrower path would never match.
#: Unlike HybridInference's ``/auth`` path the entry rides on every request;
#: it carries no authority, and every read of it checks its signature.
COOKIE_PATH = "/"
#: ``__Host-`` wherever the cookie is ``Secure``, as the session cookie.
COOKIE_NAME = "__Host-cloud_agent_device"
COOKIE_NAME_INSECURE = "cloud_agent_device"


def cookie_name() -> str:
    """The name the entry is set and read under on this deployment."""
    return COOKIE_NAME if session_cookie_is_secure() else COOKIE_NAME_INSECURE


_NONCE_BYTES = 16
_TAG_BYTES = hashlib.sha256().digest_size
_EXPIRY = struct.Struct(">I")
# An account signs in with an email address, a login name, or either of both.
_MAX_TAGS = 2
_HEADER_BYTES = _NONCE_BYTES + _EXPIRY.size
_MAX_ENTRY_CHARS = -(-(_HEADER_BYTES + _MAX_TAGS * _TAG_BYTES) * 4 // 3)


@dataclass(frozen=True)
class _Entry:
    nonce: bytes
    expires: int
    tags: tuple[bytes, ...]


def _now() -> int:
    return int(time.time())


def _key() -> bytes | None:
    try:
        return derived_key("device-cookie")
    except LocalAuthNotConfigured:
        return None


def _tag(key: bytes, nonce: bytes, expires: int, identifier: str) -> bytes:
    # Nonce and expiry are fixed-length, so the identifier is the remainder.
    return hmac.new(key, nonce + _EXPIRY.pack(expires) + identifier.encode(), "sha256").digest()


def _matches(key: bytes, entry: _Entry, identifier: str) -> bool:
    expected = _tag(key, entry.nonce, entry.expires, identifier)
    matched = False
    for tag in entry.tags:
        matched |= hmac.compare_digest(expected, tag)
    return matched


def _encode(entry: _Entry) -> str:
    raw = entry.nonce + _EXPIRY.pack(entry.expires) + b"".join(entry.tags)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> _Entry | None:
    if not text or len(text) > _MAX_ENTRY_CHARS:
        return None
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    tags = raw[_HEADER_BYTES:]
    if not tags or len(tags) % _TAG_BYTES or len(tags) > _MAX_TAGS * _TAG_BYTES:
        return None
    (expires,) = _EXPIRY.unpack_from(raw, _NONCE_BYTES)
    return _Entry(
        nonce=raw[:_NONCE_BYTES],
        expires=expires,
        tags=tuple(tags[i : i + _TAG_BYTES] for i in range(0, len(tags), _TAG_BYTES)),
    )


def _entries(value: str | None) -> Iterator[_Entry]:
    if not value:
        return
    for text in value.split(".", MAX_ENTRIES)[:MAX_ENTRIES]:
        entry = _decode(text)
        if entry is not None:
            yield entry


def recognized_device(cookies: Mapping[str, str], identifier_key: str) -> str | None:
    """The rate-limit key of the request's valid entry for an identifier, if any.

    ``identifier_key`` is the sign-in limiter's key for what the client signs
    in with (:func:`cloud_agent.application.accounts.identifier_key`).
    """
    value = cookies.get(cookie_name())
    key = _key() if value else None
    if key is None:
        return None
    now = _now()
    for entry in _entries(value):
        if entry.expires > now and _matches(key, entry, identifier_key):
            return entry.nonce.hex()
    return None


def remember_device(
    response: Response, identifier_keys: list[str], cookies: Mapping[str, str] | None
) -> None:
    """Set the cookie with a fresh entry for one account ahead of the browser's others.

    Called on every successful password sign-in, so the account's entry is
    rotated. The request's other unexpired entries follow, newest first, up to
    :data:`MAX_ENTRIES`. Nothing is set without an identifier or the key.
    """
    identifiers = list(dict.fromkeys(i for i in identifier_keys if i))[:_MAX_TAGS]
    key = _key()
    if not identifiers or key is None:
        return
    now = _now()
    expires = now + LIFETIME_SECONDS
    nonce = secrets.token_bytes(_NONCE_BYTES)
    entries = [_Entry(nonce, expires, tuple(_tag(key, nonce, expires, i) for i in identifiers))]
    for entry in _entries((cookies or {}).get(cookie_name())):
        if entry.expires > now and not any(_matches(key, entry, i) for i in identifiers):
            entries.append(entry)
    response.set_cookie(
        cookie_name(),
        ".".join(_encode(entry) for entry in entries[:MAX_ENTRIES]),
        max_age=LIFETIME_SECONDS,
        path=COOKIE_PATH,
        secure=session_cookie_is_secure(),
        httponly=True,
        samesite="lax",
    )


__all__ = [
    "COOKIE_NAME",
    "COOKIE_NAME_INSECURE",
    "COOKIE_PATH",
    "MAX_ENTRIES",
    "cookie_name",
    "recognized_device",
    "remember_device",
]
