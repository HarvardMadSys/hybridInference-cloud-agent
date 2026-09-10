"""Identity verification and sessions.

Every test here is about a refusal. The service's job in this area is to be
hard to fool, and the failures that matter are the ones where a token *nearly*
qualifies: right shape, wrong audience; right issuer, wrong key; valid once,
replayed after expiry.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient as RealPyJWKClient
from jwt.algorithms import RSAAlgorithm

from cloud_agent import identity
from cloud_agent.identity import (
    AUDIENCE,
    ENV_JWKS_URL,
    ENV_SESSION_SECRET,
    IdentityError,
    IdentityNotConfigured,
    mint_session,
    verify_identity_token,
    verify_session,
)

ISSUER = "https://gateway.example"
# 32+ bytes: the module refuses anything shorter, and a fixture that did not
# meet its own minimum would test a path deployments cannot use.
SESSION_SECRET = "session-signing-secret-long-enough-for-hs256"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key, pem


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    identity.reset_jwks_cache()
    monkeypatch.setenv("GATEWAY_ISSUER", ISSUER)
    monkeypatch.delenv(ENV_JWKS_URL, raising=False)
    monkeypatch.delenv(ENV_SESSION_SECRET, raising=False)
    yield
    identity.reset_jwks_cache()


def _token(
    key,
    *,
    aud: str = AUDIENCE,
    iss: str = ISSUER,
    exp_delta: int = 600,
    kid: str | None = None,
    **extra,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user_1",
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_delta,
        "role": "pro",
        **extra,
    }
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(claims, key, algorithm="RS256", headers=headers)


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


def _serve_key(monkeypatch, key) -> None:
    """Stand in for the JWKS fetch with the public half of `key`."""
    monkeypatch.setenv(ENV_JWKS_URL, "https://gateway.example/v1/identity/jwks")

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str):
            return _FakeSigningKey(key.public_key())

    monkeypatch.setattr(identity, "PyJWKClient", _Client)


# ---------------------------------------------------------------------------
# Identity tokens
# ---------------------------------------------------------------------------


def test_a_valid_token_verifies(monkeypatch, keypair) -> None:
    key, _ = keypair
    _serve_key(monkeypatch, key)
    claims = verify_identity_token(_token(key))
    assert claims["sub"] == "user_1"
    assert "role" not in claims


def test_a_token_for_another_audience_is_refused(monkeypatch, keypair) -> None:
    """A credential for a different consumer must not act as ours."""
    key, _ = keypair
    _serve_key(monkeypatch, key)
    with pytest.raises(IdentityError):
        verify_identity_token(_token(key, aud="some-other-service"))


def test_an_expired_token_is_refused(monkeypatch, keypair) -> None:
    key, _ = keypair
    _serve_key(monkeypatch, key)
    with pytest.raises(IdentityError):
        verify_identity_token(_token(key, exp_delta=-1))


def test_a_token_signed_by_another_key_is_refused(monkeypatch, keypair) -> None:
    key, _ = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _serve_key(monkeypatch, key)
    with pytest.raises(IdentityError):
        verify_identity_token(_token(other))


def test_a_token_without_a_subject_is_refused(monkeypatch, keypair) -> None:
    """`sub` is the only thing this service stores about a person."""
    key, _ = keypair
    _serve_key(monkeypatch, key)
    now = int(time.time())
    token = jwt.encode({"aud": AUDIENCE, "iss": ISSUER, "exp": now + 600}, key, algorithm="RS256")
    with pytest.raises(IdentityError):
        verify_identity_token(token)


def test_an_unfetchable_key_refuses_rather_than_passing(monkeypatch, keypair) -> None:
    """Failing open here would accept any token during a gateway outage."""
    key, _ = keypair
    monkeypatch.setenv(ENV_JWKS_URL, "https://gateway.example/v1/identity/jwks")

    class _Broken:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str):
            raise RuntimeError("jwks unreachable")

    monkeypatch.setattr(identity, "PyJWKClient", _Broken)
    with pytest.raises(IdentityError):
        verify_identity_token(_token(key))


def test_a_withdrawn_jwks_key_stops_verifying_after_the_set_refreshes(monkeypatch) -> None:
    """The bounded JWKS-set cache must refresh without an immortal key cache."""
    first = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def public_jwk(key, kid: str) -> dict[str, Any]:
        value = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        value.update({"kid": kid, "use": "sig"})
        return value

    served = {"keys": [public_jwk(first, "first")]}

    class _RotatingClient(RealPyJWKClient):
        def fetch_data(self) -> dict[str, Any]:
            data = {"keys": list(served["keys"])}
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(data)
            return data

    monkeypatch.setattr(identity, "PyJWKClient", _RotatingClient)
    monkeypatch.setenv(ENV_JWKS_URL, "https://gateway.example/v1/identity/jwks")

    assert verify_identity_token(_token(first, kid="first"))["sub"] == "user_1"
    served["keys"] = [public_jwk(second, "second")]
    clock["now"] += identity.JWKS_CACHE_LIFESPAN_S + 1

    with pytest.raises(IdentityError):
        verify_identity_token(_token(first, kid="first"))
    assert verify_identity_token(_token(second, kid="second"))["sub"] == "user_1"


def test_no_jwks_url_is_not_configured(keypair) -> None:
    key, _ = keypair
    with pytest.raises(IdentityNotConfigured):
        verify_identity_token(_token(key))


def test_an_empty_token_is_refused() -> None:
    with pytest.raises(IdentityError):
        verify_identity_token("")


def test_the_issuer_must_match_the_configured_gateway(monkeypatch, keypair) -> None:
    key, _ = keypair
    _serve_key(monkeypatch, key)
    monkeypatch.setenv("GATEWAY_ISSUER", ISSUER)
    assert verify_identity_token(_token(key))["sub"] == "user_1"
    with pytest.raises(IdentityError):
        verify_identity_token(_token(key, iss="https://impostor.example"))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_a_session_round_trips(monkeypatch) -> None:
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    claims = verify_session(mint_session(user_id="user_1"))
    assert claims["sub"] == "user_1"
    assert "role" not in claims


def test_a_session_contains_only_minimal_identity(monkeypatch) -> None:
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    assert set(verify_session(mint_session(user_id="user_1"))) == {
        "sub",
        "gateway_issuer",
        "iat",
        "exp",
    }


def test_a_session_signed_with_another_secret_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    token = mint_session(user_id="user_1")
    monkeypatch.setenv(ENV_SESSION_SECRET, "a-different-secret-also-long-enough-x")
    with pytest.raises(IdentityError):
        verify_session(token)


def test_an_expired_session_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    now = int(time.time())
    token = jwt.encode({"sub": "user_1", "exp": now - 1}, SESSION_SECRET, algorithm="HS256")
    with pytest.raises(IdentityError):
        verify_session(token)


def test_no_session_secret_is_not_configured() -> None:
    """Refusing beats defaulting: a shared default forges every deployment."""
    with pytest.raises(IdentityNotConfigured):
        mint_session(user_id="user_1")


def test_a_short_session_secret_is_refused(monkeypatch) -> None:
    """The JWT library only warns about this, and a warning is not a control.

    A secret below the HS256 minimum is brute-forceable, and every session it
    signs is forgeable — so the refusal happens here rather than in a log line
    nobody reads.
    """
    monkeypatch.setenv(ENV_SESSION_SECRET, "too-short")
    with pytest.raises(IdentityNotConfigured, match="at least"):
        mint_session(user_id="user_1")


def test_an_identity_token_is_not_accepted_as_a_session(monkeypatch, keypair) -> None:
    """Different algorithms and different secrets, deliberately.

    An RS256 identity token presented as a session must not verify — otherwise
    the short-lived credential meant to be spent once would work for a week.
    """
    key, _ = keypair
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    with pytest.raises(IdentityError):
        verify_session(_token(key))


def test_the_jwks_fetch_identifies_itself(monkeypatch) -> None:
    """`Python-urllib/3.x` is a User-Agent CDNs refuse, and a gateway is behind one.

    PyJWKClient fetches with `urllib`, whose default UA Cloudflare answers 403
    — while the same URL answered 200 to `httpx` from the same container, and
    `httpx` is what every other gateway call in this service uses. Two clients,
    one host, different answers.

    What that cost, on staging: sign-in returned 401 "Invalid or expired token"
    against a token that had just been minted and was entirely valid. The key
    to check it against could not be fetched, and this module's caller does not
    echo the reason on purpose — so nothing in either service's logs named the
    cause.
    """
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, uri, **kwargs):
            captured["uri"] = uri
            captured.update(kwargs)

    monkeypatch.setattr(identity, "PyJWKClient", _Client)
    monkeypatch.setenv(identity.ENV_JWKS_URL, "https://gateway.example/v1/identity/jwks")
    identity.reset_jwks_cache()
    try:
        identity._jwks_client()
    finally:
        identity.reset_jwks_cache()

    agent = str((captured.get("headers") or {}).get("User-Agent", ""))
    assert agent, "the JWKS fetch sends no User-Agent, so urllib's default is used"
    assert "urllib" not in agent.lower(), agent
    assert "cloud-agent" in agent.lower(), (
        "the User-Agent should name this service, so an operator reading a "
        f"gateway's access log can tell who was refused; got {agent!r}"
    )


def test_identity_profiles_are_discarded_even_from_an_older_gateway(monkeypatch, keypair):
    key, _ = keypair
    _serve_key(monkeypatch, key)
    claims = verify_identity_token(_token(key, email="sentinel@example.test", user_name="Sentinel"))
    assert set(claims) == {"iss", "aud", "sub", "iat", "exp"}


def test_a_session_without_the_namespace_is_refused(monkeypatch):
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    legacy = jwt.encode(
        {
            "sub": "user_1",
            "role": "admin",
            "email": "sentinel@example.test",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        SESSION_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(IdentityError):
        verify_session(legacy)


def test_changing_gateway_rejects_old_sessions_even_with_the_same_secret(monkeypatch):
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    token = mint_session(user_id="same-user-id")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://other.example")
    with pytest.raises(IdentityError, match="another Gateway"):
        verify_session(token)


def test_session_profiles_are_rejected_instead_of_retained(monkeypatch):
    monkeypatch.setenv(ENV_SESSION_SECRET, SESSION_SECRET)
    claims = verify_session(mint_session(user_id="user_1"))
    claims["email"] = "sentinel@example.test"
    token = jwt.encode(claims, SESSION_SECRET, algorithm="HS256")
    with pytest.raises(IdentityError, match="unsupported session claims"):
        verify_session(token)


def test_issuer_is_required_even_when_the_identity_signature_verifies(monkeypatch, keypair):
    key, _ = keypair
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    with pytest.raises(IdentityNotConfigured, match="GATEWAY_ISSUER"):
        verify_identity_token(_token(key))
