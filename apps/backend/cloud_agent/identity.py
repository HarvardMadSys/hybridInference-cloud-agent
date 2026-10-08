"""Verifying the gateway's identity tokens, and minting our own session.

Two credentials, deliberately different lifetimes and audiences.

The **identity token** is the gateway's: RS256, short-lived, `aud=cloud-agent`,
verified against its JWKS. It says who a person is and nothing about what they
may do here. This service never holds the key that signs it — only the public
half, fetched from a URL — which is the property that lets the two services
share a login without sharing a secret.

The **session cookie** is ours: HttpOnly, signed with our own secret, and the
only credential the browser keeps. The identity token is exchanged for one and
then discarded, so a token intercepted afterwards is already spent.

**The JWKS is cached briefly and re-fetched on a miss.** That is what makes
rotation work without coordination: the gateway starts signing with a new key,
the first token carrying an unknown ``kid`` triggers one fetch, and tokens
already in flight keep verifying while the retiring key remains published.
Individual keys are not cached beyond the JWKS lifetime, so withdrawing one
actually revokes it after the bounded cache expires.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

ENV_JWKS_URL = "GATEWAY_JWKS_URL"
ENV_ISSUER = "GATEWAY_ISSUER"
ENV_SESSION_SECRET = "AGENT_SESSION_SECRET"

#: Must match what the gateway mints. A token for a different audience is a
#: token for a different service, and accepting it would let one consumer's
#: credential act as another's.
AUDIENCE = "cloud-agent"

#: How this service identifies itself on outbound HTTP.
#:
#: Named rather than left to each library's default because the defaults
#: differ: `httpx` announces itself in a shape CDNs accept, `urllib` announces
#: `Python-urllib/3.x`, which they routinely refuse. Two clients in one service
#: reaching one host and getting different answers is the bug this prevents.
USER_AGENT = "hybridinference-cloud-agent"

#: Keep retired signing keys usable only for the gateway's published overlap.
#: Passed explicitly to PyJWT so a dependency-default change cannot silently
#: turn the intended bounded revocation window into an unbounded one.
JWKS_CACHE_LIFESPAN_S = 5 * 60

#: Our own identity session, with a fixed thirty-day expiry.
#:
#: Every protected request resolves Agent permissions afresh at the Gateway.
#: No role or permission in a session is an authorization cache, so revoking
#: someone's Agent access takes effect on their next request however old the
#: cookie is. The expiry bounds the identity credential's lifetime and is never
#: extended on use. Nothing revokes a session before then: signing out drops
#: only the browser's copy, and rotating ``AGENT_SESSION_SECRET`` ends them all.
SESSION_TTL_S = 30 * 24 * 60 * 60
SESSION_COOKIE = "cloud_agent_session"

#: The parked half of a sign-in: the PKCE verifier and the CSRF ``state``,
#: signed and handed to the browser as an HttpOnly cookie for the length of one
#: round trip to the gateway's consent page.
#:
#: **A cookie rather than a server-side table** because it is exactly one
#: round trip's worth of state, it is useless to anyone who cannot also present
#: it back, and a table would need reaping. **HttpOnly rather than
#: ``localStorage``** because that is the whole point: page JavaScript cannot
#: read it, so an XSS cannot walk off with a verifier and complete somebody
#: else's login.
LOGIN_FLOW_COOKIE = "cloud_agent_login"

#: How long a parked sign-in stays valid. Long enough to read a consent screen,
#: short enough that an abandoned flow is not a credential lying around.
LOGIN_FLOW_TTL_S = 10 * 60


class IdentityError(Exception):
    """An identity token or session could not be verified."""


class IdentityNotConfigured(IdentityError):
    """This deployment cannot verify identity at all."""


_jwk_client: PyJWKClient | None = None
_jwk_client_url: str = ""


def _jwks_client() -> PyJWKClient:
    """Return the JWKS client, built once per URL.

    Raises:
        IdentityNotConfigured: If no JWKS URL is set.
    """
    global _jwk_client, _jwk_client_url

    url = (os.environ.get(ENV_JWKS_URL) or "").strip()
    if not url:
        raise IdentityNotConfigured(f"{ENV_JWKS_URL} must be set to verify identity tokens")
    if _jwk_client is None or _jwk_client_url != url:
        # Cache the JWKS set for an explicit bounded lifetime, and re-fetch on
        # an unknown kid. Do not enable PyJWT's separate per-key LRU: it has
        # no expiry and would keep a withdrawn signing key trusted until this
        # process restarted.
        #
        # **The User-Agent is not decoration.** PyJWKClient fetches with
        # `urllib`, which announces itself as `Python-urllib/3.x` — and a
        # gateway behind a CDN or WAF is entitled to refuse that. Cloudflare
        # does: on staging this exact request answered 403 while the same URL
        # answered 200 to `httpx` from the same container, which is what every
        # other gateway call in this service uses.
        #
        # The failure that produced is worth remembering. Sign-in returned 401
        # "Invalid or expired token" — the token was fine and had just been
        # minted; the key to check it against could not be fetched. Nothing in
        # either service's logs named a CDN, because from here it was a 403 on
        # an internal call and from there it was a request that never arrived.
        _jwk_client = PyJWKClient(
            url,
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=JWKS_CACHE_LIFESPAN_S,
            headers={"User-Agent": USER_AGENT},
        )
        _jwk_client_url = url
    return _jwk_client


def reset_jwks_cache() -> None:
    """Drop the cached JWKS client. For tests that change the URL."""
    global _jwk_client, _jwk_client_url
    _jwk_client = None
    _jwk_client_url = ""


def gateway_issuer() -> str:
    """Return the explicitly configured identity issuer, never the transport URL."""
    issuer = (os.environ.get(ENV_ISSUER) or "").strip()
    if not issuer:
        raise IdentityNotConfigured(f"{ENV_ISSUER} must be set to verify Gateway identity")
    return issuer


def verify_identity_token(token: str) -> dict[str, Any]:
    """Verify a gateway identity token and return its claims.

    Args:
        token: The RS256 JWT the gateway issued.

    Returns:
        The verified claims.

    Raises:
        IdentityNotConfigured: If this deployment has no JWKS URL or issuer.
        IdentityError: If the token is missing, malformed, expired, signed by
            an unknown key, or issued by another issuer or for another audience.
    """
    if not token:
        raise IdentityError("no identity token presented")
    issuer = gateway_issuer()
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
    except IdentityNotConfigured:
        raise
    except (jwt.PyJWTError, httpx.HTTPError, Exception) as exc:
        # An unfetchable or unknown key is a refusal, never a pass. Failing
        # open here would accept any token during a gateway outage.
        raise IdentityError(f"cannot resolve the signing key: {exc}") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=issuer,
            options={"require": ["iss", "aud", "iat", "exp", "sub"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise IdentityError("invalid identity subject")
        # Older Gateways may still send profile claims during rollout. Never
        # copy them into this service's identity context, API or session.
        return {key: claims[key] for key in ("iss", "aud", "sub", "iat", "exp")}
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid identity token: {exc}") from exc


#: RFC 7518 §3.2 for HS256. PyJWT only *warns* below this, and a warning in a
#: deploy log is not a control — a short secret is brute-forceable, and every
#: session it signs is forgeable.
MIN_SESSION_SECRET_BYTES = 32


def _session_secret() -> str:
    """Return the session signing secret.

    Raises:
        IdentityNotConfigured: If unset or too short. Refusing beats
            defaulting — a default would make every deployment able to forge
            every other's sessions — and refusing beats warning, which is what
            the JWT library does on its own.
    """
    secret = (os.environ.get(ENV_SESSION_SECRET) or "").strip()
    if not secret:
        raise IdentityNotConfigured(f"{ENV_SESSION_SECRET} must be set to issue sessions")
    if len(secret.encode()) < MIN_SESSION_SECRET_BYTES:
        raise IdentityNotConfigured(
            f"{ENV_SESSION_SECRET} must be at least {MIN_SESSION_SECRET_BYTES} bytes; "
            "generate one with `openssl rand -base64 32`"
        )
    return secret


def mint_session(*, user_id: str) -> str:
    """Mint a minimal Agent session bound to the configured Gateway issuer."""
    if not isinstance(user_id, str) or not user_id:
        raise IdentityError("invalid session subject")
    now = int(time.time())
    claims = {
        "sub": user_id,
        "gateway_issuer": gateway_issuer(),
        "iat": now,
        "exp": now + SESSION_TTL_S,
    }
    return jwt.encode(claims, _session_secret(), algorithm="HS256")


def verify_session(token: str) -> dict[str, Any]:
    """Verify a minimal session and reject legacy or foreign-issuer cookies."""
    if not token:
        raise IdentityError("no session presented")
    try:
        claims = jwt.decode(
            token,
            _session_secret(),
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "gateway_issuer"]},
        )
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid session: {exc}") from exc
    if set(claims) != {"sub", "gateway_issuer", "iat", "exp"}:
        raise IdentityError("unsupported session claims; sign in again")
    if claims["gateway_issuer"] != gateway_issuer():
        raise IdentityError("session belongs to another Gateway")
    return claims


#: Domain separation for the things this service signs. All are HS256 under one
#: configured secret, and a login-flow token that could be presented as a
#: session (or the reverse) is a confused deputy waiting to happen. Keys
#: derived from the one secret under their own purpose make that structurally
#: impossible instead of relying on a claim being checked everywhere.
_LOGIN_FLOW_KEY_INFO = b"cloud-agent/login-flow/v1"

#: Purpose for admin pagination cursors over per-request gateway usage. A
#: cursor is not a credential: it carries no authority of its own, and every
#: page re-authenticates and re-checks attribution. Signing only stops a
#: browser from steering a continuation at another query.
_ADMIN_LLM_REQUEST_CURSOR_KEY_INFO = b"cloud-agent/admin-llm-requests-cursor/v1"


def _login_flow_secret() -> str:
    """Return the key the parked-sign-in cookie is signed with.

    Raises:
        IdentityNotConfigured: If no session secret is set.
    """
    return hmac.new(_session_secret().encode(), _LOGIN_FLOW_KEY_INFO, hashlib.sha256).hexdigest()


def admin_llm_request_cursor_secret() -> str:
    """Return the key an admin request-page continuation cursor is signed with.

    Named for its one purpose rather than exposed as a general "derive a key"
    helper: a second caller with a second purpose adds its own constant, so no
    two signed shapes can ever be confused.

    Returns:
        The derived signing key.

    Raises:
        IdentityNotConfigured: If no session secret is set.
    """
    return hmac.new(
        _session_secret().encode(), _ADMIN_LLM_REQUEST_CURSOR_KEY_INFO, hashlib.sha256
    ).hexdigest()


def new_verifier() -> str:
    """Return a fresh PKCE code verifier.

    Returns:
        43 characters of base64url, the minimum RFC 7636 allows and the
        minimum the gateway's ``code_verifier`` field accepts.
    """
    # 32 bytes → 43 base64url characters with no padding.
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def challenge_for(verifier: str) -> str:
    """Return the S256 challenge for a verifier.

    Args:
        verifier: The PKCE code verifier.

    Returns:
        The base64url-encoded SHA-256 of the verifier, unpadded.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def new_state() -> str:
    """Return a fresh CSRF ``state`` value."""
    return secrets.token_urlsafe(32)


def mint_login_flow(*, verifier: str, state: str, return_to: str) -> str:
    """Park one in-flight sign-in in a signed, short-lived token.

    Args:
        verifier: The PKCE verifier the callback will redeem the code with.
        state: The CSRF value the callback must see come back.
        return_to: Where in the app to land afterwards. Already validated as a
            same-site path by the caller.

    Returns:
        A signed token for the login-flow cookie.

    Raises:
        IdentityNotConfigured: If no session secret is set.
    """
    now = int(time.time())
    claims = {
        "verifier": verifier,
        "state": state,
        "return_to": return_to,
        "iat": now,
        "exp": now + LOGIN_FLOW_TTL_S,
    }
    return jwt.encode(claims, _login_flow_secret(), algorithm="HS256")


def verify_login_flow(token: str) -> dict[str, Any]:
    """Verify a parked sign-in and return its claims.

    Args:
        token: The login-flow cookie's value.

    Returns:
        The verified claims.

    Raises:
        IdentityNotConfigured: If no session secret is set.
        IdentityError: If the token is missing, malformed, or expired.
    """
    if not token:
        raise IdentityError("no sign-in is in progress")
    try:
        return jwt.decode(
            token,
            _login_flow_secret(),
            algorithms=["HS256"],
            options={"require": ["exp", "state", "verifier"]},
        )
    except jwt.PyJWTError as exc:
        raise IdentityError(f"invalid sign-in state: {exc}") from exc


def state_matches(presented: str, expected: str) -> bool:
    """Whether the returned ``state`` is the one this flow was started with.

    Constant-time: the comparison is of a secret this service minted, and a
    timing oracle on it would let a caller discover a valid ``state`` a
    character at a time.

    Args:
        presented: The ``state`` the callback arrived with.
        expected: The ``state`` parked at login.

    Returns:
        Whether they match. Empty values never match.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)
