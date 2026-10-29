"""Which identity source this deployment uses, and the local-account settings.

``AGENT_AUTH_MODE`` is read from the process environment only, never from
saved Admin Settings: switching identity source is a deployment decision with a
database migration behind it, not a toggle an administrator flips at runtime.
Any value other than ``gateway`` (the default) or ``local`` stops startup.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cloud_agent.settings import setting_env

AuthMode = Literal["gateway", "local"]

ENV_AUTH_MODE = "AGENT_AUTH_MODE"

#: Signs nothing and is shared with nothing: every local session, email token
#: and abuse-counter key is a keyed hash under a purpose-specific key derived
#: from it. It must differ from ``AGENT_SESSION_SECRET``, the Gateway-mode
#: session key, so neither mode's material can stand in for the other's.
ENV_LOCAL_SESSION_SECRET = "AGENT_LOCAL_SESSION_SECRET"

#: Public sign-up's initial value (now the ``accounts.signup_enabled`` admin
#: setting). On by default in local mode; signing up never grants Agent use.
ENV_SIGNUP_ENABLED = "AGENT_LOCAL_SIGNUP_ENABLED"

#: The web app's public origin and base path. Mail links are built from this
#: alone, never from a request's ``Host`` or forwarding headers.
ENV_WEB_BASE_URL = "AGENT_WEB_BASE_URL"

MIN_SECRET_BYTES = 32

#: The local session cookie. Its own name, so a Gateway-mode session cookie is
#: never read as a local one; ``__Host-`` binds it to this host over HTTPS.
LOCAL_SESSION_COOKIE = "__Host-cloud_agent_local_session"
#: The plain-HTTP development name, used only where the cookie is not Secure.
LOCAL_SESSION_COOKIE_INSECURE = "cloud_agent_local_session"

#: A local session's fixed lifetime. It is never extended on use; revocation
#: (sign-out, password reset, suspension) ends it before then.
LOCAL_SESSION_TTL_S = 30 * 24 * 60 * 60

#: How long an emailed link stays usable.
VERIFY_EMAIL_TTL_S = 24 * 60 * 60
RESET_PASSWORD_TTL_S = 60 * 60


class LocalAuthNotConfigured(RuntimeError):
    """Local mode is selected but a setting it needs is missing or unsafe."""


def auth_mode() -> AuthMode:
    """Return the configured identity source.

    Raises:
        LocalAuthNotConfigured: The variable holds something other than the
            two supported values. Guessing would pick an identity source.
    """
    raw = (os.environ.get(ENV_AUTH_MODE) or "gateway").strip().lower()
    if raw == "gateway":
        return "gateway"
    if raw == "local":
        return "local"
    raise LocalAuthNotConfigured(f"{ENV_AUTH_MODE} must be 'gateway' or 'local'")


def local_mode() -> bool:
    """Whether this process serves local accounts instead of Gateway sign-in."""
    return auth_mode() == "local"


def _local_secret() -> bytes:
    secret = (os.environ.get(ENV_LOCAL_SESSION_SECRET) or "").strip()
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise LocalAuthNotConfigured(
            f"{ENV_LOCAL_SESSION_SECRET} must be set to at least {MIN_SECRET_BYTES} bytes; "
            "generate one with `openssl rand -base64 32`"
        )
    gateway_secret = (setting_env("AGENT_SESSION_SECRET") or "").strip()
    if gateway_secret and gateway_secret == secret:
        raise LocalAuthNotConfigured(
            f"{ENV_LOCAL_SESSION_SECRET} must differ from AGENT_SESSION_SECRET"
        )
    return secret.encode()


@lru_cache(maxsize=32)
def _derived(secret: bytes, purpose: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"cloud-agent/local-accounts/{purpose}/v1".encode(),
    ).derive(secret)


def derived_key(purpose: str) -> bytes:
    """A 256-bit key for one purpose, derived from the local session secret.

    Raises:
        LocalAuthNotConfigured: The secret is missing, short or reused.
    """
    return _derived(_local_secret(), purpose)


def check_local_configuration() -> None:
    """Refuse to start local mode without its secret.

    Raises:
        LocalAuthNotConfigured: Something local mode needs is unusable.
    """
    _local_secret()


def signup_flag_enabled() -> bool:
    """Whether an administrator left public sign-up on (``accounts.signup_enabled``)."""
    from cloud_agent.settings import boolean_setting

    return boolean_setting("accounts.signup_enabled")


def mail_web_base_url(*, allow_http: bool) -> str:
    """The configured web base for emailed links, without a trailing slash.

    Raises:
        LocalAuthNotConfigured: Unset, relative, carrying a query or fragment,
            or plain HTTP where the session cookie is Secure.
    """
    raw = (setting_env(ENV_WEB_BASE_URL) or "").strip().rstrip("/")
    try:
        parts = urlsplit(raw)
    except ValueError:
        parts = None
    schemes = ("https", "http") if allow_http else ("https",)
    if (
        parts is None
        or parts.scheme not in schemes
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise LocalAuthNotConfigured(
            f"{ENV_WEB_BASE_URL} must be this deployment's absolute "
            f"{' or '.join(schemes)} web address to send account email"
        )
    return raw


__all__ = [
    "ENV_AUTH_MODE",
    "ENV_LOCAL_SESSION_SECRET",
    "ENV_SIGNUP_ENABLED",
    "ENV_WEB_BASE_URL",
    "LOCAL_SESSION_COOKIE",
    "LOCAL_SESSION_COOKIE_INSECURE",
    "LOCAL_SESSION_TTL_S",
    "RESET_PASSWORD_TTL_S",
    "VERIFY_EMAIL_TTL_S",
    "AuthMode",
    "LocalAuthNotConfigured",
    "auth_mode",
    "check_local_configuration",
    "derived_key",
    "local_mode",
    "mail_web_base_url",
    "signup_flag_enabled",
]
