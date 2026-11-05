"""The account system's deployment settings.

Cloud Agent has one account system, in its own database. Configuration left
over from the removed Gateway sign-in is refused at startup rather than
reinterpreted: see :func:`refuse_retired_configuration`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cloud_agent.settings import setting_env

#: Signs nothing and is shared with nothing: every session, email token and
#: abuse-counter key is a keyed hash under a purpose-specific key derived from
#: it. It must differ from ``AGENT_SESSION_SECRET``, the fallback root for
#: encrypted settings, so neither key's material can stand in for the other's.
ENV_LOCAL_SESSION_SECRET = "AGENT_LOCAL_SESSION_SECRET"

#: Public sign-up's initial value (now the ``accounts.signup_enabled`` admin
#: setting). On by default; signing up never grants Agent use.
ENV_SIGNUP_ENABLED = "AGENT_LOCAL_SIGNUP_ENABLED"

#: The web app's public origin and base path. Mail links are built from this
#: alone, never from a request's ``Host`` or forwarding headers.
ENV_WEB_BASE_URL = "AGENT_WEB_BASE_URL"

MIN_SECRET_BYTES = 32

#: The session cookie. ``__Host-`` binds it to this host over HTTPS. The name is
#: unchanged from the account system's first release, so sessions survive an
#: upgrade.
LOCAL_SESSION_COOKIE = "__Host-cloud_agent_local_session"
#: The plain-HTTP development name, used only where the cookie is not Secure.
LOCAL_SESSION_COOKIE_INSECURE = "cloud_agent_local_session"

#: ``0``, ``false`` or ``no`` drops ``Secure`` from the session and device
#: cookies, for plain-HTTP development only.
ENV_SESSION_COOKIE_SECURE = "AGENT_SESSION_COOKIE_SECURE"


def session_cookie_is_secure() -> bool:
    """Whether the session and device cookies carry ``Secure``, as by default."""
    raw = (setting_env(ENV_SESSION_COOKIE_SECURE) or "").strip().lower()
    return raw not in ("0", "false", "no")


#: A local session's default fixed lifetime (``accounts.session_ttl_days``). It
#: is never extended on use; revocation (sign-out, password change or reset,
#: suspension, deletion) ends it before then.
LOCAL_SESSION_TTL_S = 30 * 24 * 60 * 60


def local_session_ttl_s() -> int:
    """The configured lifetime of a new local session, in seconds."""
    from cloud_agent.settings import integer_setting

    return integer_setting("accounts.session_ttl_days") * 24 * 60 * 60


#: How long an emailed link stays usable.
VERIFY_EMAIL_TTL_S = 24 * 60 * 60
RESET_PASSWORD_TTL_S = 60 * 60


class LocalAuthNotConfigured(RuntimeError):
    """A setting the account system needs is missing or unsafe."""


#: Where an operator reads how to move a Gateway-era deployment to accounts.
CUTOVER_RUNBOOK = "docs/developer/upgrade-notes.md#independent-accounts-cutover"

#: Variables that configured the removed Gateway sign-in, admission and
#: inference. Each one set means the deployment still expects Gateway
#: behaviour, so startup stops instead of silently ignoring it.
RETIRED_GATEWAY_VARIABLES = (
    "AGENT_AUTH_MODE",
    "AGENT_GATEWAY_SSH_HOST",
    "AGENT_GATEWAY_URL",
    "AGENT_SESSION_REDIRECT_URI",
    "AGENT_WEB_GATEWAY_URL",
    # The documentation assistant the Gateway served; the web app has no
    # switch for it any more.
    "AGENT_WEB_FEATURE_RAG",
    "GATEWAY_AUTHORIZE_URL",
    "GATEWAY_BASE_URL",
    "GATEWAY_GRANT_BASE_PATH",
    "GATEWAY_GRANT_BINDING_ID",
    "GATEWAY_GRANT_DISPATCH_TOKEN",
    "GATEWAY_ISSUER",
    "GATEWAY_JWKS_URL",
    "IDENTITY_ALLOWED_REDIRECTS",
    "IDENTITY_ISSUER",
    "IDENTITY_JWT_PRIVATE_KEY",
    "IDENTITY_JWT_RETIRING_PUBLIC_KEYS",
    "AGENT_ONCALL_DISPATCH_TOKEN",
    "AGENT_ONCALL_USER_ID",
)


class RetiredConfiguration(LocalAuthNotConfigured):
    """The environment still configures the removed Gateway integration."""


def retired_configuration(environ: dict[str, str] | None = None) -> list[str]:
    """The retired Gateway variables set (non-empty) in ``environ``."""
    env = os.environ if environ is None else environ
    return [name for name in RETIRED_GATEWAY_VARIABLES if (env.get(name) or "").strip()]


def refuse_retired_configuration(environ: dict[str, str] | None = None) -> None:
    """Stop startup while any retired Gateway variable is set.

    Raises:
        RetiredConfiguration: Names every such variable and the runbook. An
            ``AGENT_AUTH_MODE=local`` left from before is refused too: there
            is no mode to select, and accepting it would hide that the rest of
            the old configuration was never reviewed.
    """
    found = retired_configuration(environ)
    if found:
        moved = (
            " AGENT_SESSION_REDIRECT_URI was also this API's public address: set "
            "AGENT_PUBLIC_API_URL to it without /v1/session/callback."
            if "AGENT_SESSION_REDIRECT_URI" in found
            else ""
        )
        raise RetiredConfiguration(
            "Cloud Agent no longer supports Gateway sign-in or Gateway inference; "
            f"remove {', '.join(found)} from this deployment's environment. Accounts "
            "live in Cloud Agent's own database and models run on each owner's model "
            f"connections.{moved} See {CUTOVER_RUNBOOK}."
        )


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
    """Refuse to start without the account system's secret.

    Raises:
        LocalAuthNotConfigured: Something the account system needs is unusable.
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
    "CUTOVER_RUNBOOK",
    "ENV_LOCAL_SESSION_SECRET",
    "ENV_SESSION_COOKIE_SECURE",
    "ENV_SIGNUP_ENABLED",
    "ENV_WEB_BASE_URL",
    "LOCAL_SESSION_COOKIE",
    "LOCAL_SESSION_COOKIE_INSECURE",
    "LOCAL_SESSION_TTL_S",
    "RESET_PASSWORD_TTL_S",
    "RETIRED_GATEWAY_VARIABLES",
    "VERIFY_EMAIL_TTL_S",
    "LocalAuthNotConfigured",
    "RetiredConfiguration",
    "check_local_configuration",
    "derived_key",
    "local_session_ttl_s",
    "mail_web_base_url",
    "refuse_retired_configuration",
    "retired_configuration",
    "session_cookie_is_secure",
    "signup_flag_enabled",
]
