"""The session endpoints (task E8) — this service's own half of sign-in.

The identity token is verified for real here: a generated RSA key stands in for
the gateway's JWKS, so the callback is exercised against a genuine RS256 token
with a genuine audience check rather than against a stubbed verifier. What is
faked is the gateway's HTTP surface — the code exchange and the *status*
contract — which this repository does not implement either side of.

The properties worth more than the rest, each with its own case:

- **The browser never holds a bearer credential.** The verifier lives in an
  HttpOnly cookie and the identity token never leaves this process, so neither
  appears in a response body. This is the whole difference between a BFF and a
  page that happens to call an API.
- **A callback nobody started is refused before the code is spent.** Missing,
  mismatched or replayed ``state`` is login CSRF, and the check has to happen
  ahead of the exchange or the refusal costs a code anyway.
- **A suspended account does not get a cookie.** The identity token proves who
  someone is at the moment they authenticated and says nothing about whether
  they may still use anything.
- **An unreachable gateway mints nothing.** Fail-closed, per AGENTS.md rule 4:
  a status call that cannot be made is not a status call that said yes.
- **The session is hours, not days.** It bounds the identity credential's
  lifetime; admission is checked separately on every protected request.
- **A 401 from ``/me`` is the ordinary answer**, not a server error. The web
  app's ``ProtectedRoute`` loops if it ever arrives as a 5xx.
- **Sign-in and ``/me`` answer the same thing about the same person.** They are
  one contract — ``SessionUserResponse`` — and the session sign-in mints is
  exactly what ``/me`` reads.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.api import session as session_router
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio

_USER = "user_1"
_SESSION_SECRET = "session-signing-secret-long-enough-for-hs256"
_AUTHORIZE_URL = "https://gateway.example/authorize"
_REDIRECT_URI = "https://agents.example/api/v1/session/callback"
_WEB_BASE_URL = "https://agents.example"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


@pytest.fixture(autouse=True)
def _identity_env(monkeypatch, key):
    """Point identity verification at a key this test controls."""
    identity.reset_jwks_cache()
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, _SESSION_SECRET)
    monkeypatch.setenv(identity.ENV_JWKS_URL, "https://gateway.example/v1/identity/jwks")
    monkeypatch.setenv(session_router.ENV_AUTHORIZE_URL, _AUTHORIZE_URL)
    monkeypatch.setenv(session_router.ENV_REDIRECT_URI, _REDIRECT_URI)
    monkeypatch.setenv(session_router.ENV_WEB_BASE_URL, _WEB_BASE_URL)
    monkeypatch.delenv(identity.ENV_ISSUER, raising=False)

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str):
            return _FakeSigningKey(key.public_key())

    monkeypatch.setattr(identity, "PyJWKClient", _Client)
    yield
    identity.reset_jwks_cache()


def _identity_token(key, *, sub: str = _USER, aud: str = identity.AUDIENCE, **extra) -> str:
    """Mint what the gateway hands back in exchange for a code."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + 600,
        "email": "owner@example.com",
        **extra,
    }
    return jwt.encode(claims, key, algorithm="RS256")


class _FakeGateway:
    """The gateway's code-exchange and user-status contracts, in-process."""

    def __init__(
        self,
        record: dict[str, Any] | None = None,
        *,
        error: bool = False,
        identity_token: str = "",
        exchange_error: GatewayError | None = None,
        permissions: list[str] | None = None,
    ) -> None:
        self.record = record if record is not None else {"exists": True, "active": True}
        self.error = error
        self.permissions = permissions if permissions is not None else ["agent.use"]
        self.access_asked: list[str] = []
        self.identity_token = identity_token
        self.exchange_error = exchange_error
        self.asked: list[str] = []
        self.exchanged: list[dict[str, str]] = []

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        self.access_asked.append(user_id)
        if self.error:
            raise GatewayError("gateway unavailable")
        return {
            "user_id": user_id,
            "allowed": "agent.use" in self.permissions,
            "permissions": self.permissions,
        }

    async def user_status(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error:
            raise GatewayError("gateway unreachable: nope")
        return self.record

    async def exchange_authorization_code(
        self, *, code: str, verifier: str, client_id: str, redirect_uri: str
    ) -> str:
        self.exchanged.append(
            {
                "code": code,
                "verifier": verifier,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
            }
        )
        if self.exchange_error is not None:
            raise self.exchange_error
        return self.identity_token


def _app(gateway: _FakeGateway) -> FastAPI:
    app = FastAPI()
    app.include_router(session_router.router)
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


def _state_from(response) -> str:
    """Read the ``state`` the redirect to the gateway carries."""
    location = response.headers.get("location", "")
    return parse_qs(urlparse(location).query).get("state", [""])[0]


async def _begin(client, *, return_to: str = "/"):
    """Start a sign-in. Returns the redirect response."""
    return await client.get("/v1/session/login", params={"return_to": return_to})


async def _sign_in(client, *, return_to: str = "/", code: str = "the-code"):
    """Drive a whole sign-in round trip and return the callback's response."""
    started = await _begin(client, return_to=return_to)
    return await client.get(
        "/v1/session/callback", params={"code": code, "state": _state_from(started)}
    )


@pytest_asyncio.fixture()
async def client_and_gateway(key):
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"},
        identity_token=_identity_token(key),
    )
    transport = ASGITransport(app=_app(gateway))
    # https, because the cookie carries ``Secure`` and a client talking plain
    # http would silently drop it — which would make every case below pass for
    # the wrong reason, or fail for one. Redirects are not followed: the first
    # hop is an external consent page this transport cannot serve.
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        yield client, gateway


@pytest_asyncio.fixture()
async def client_and_gateway_named(key):
    """A gateway whose status record carries a display name.

    Hypothetical today — the contract has no such field — which is the point:
    the plumbing has to be right before the field exists, or it arrives and
    only one of the two routes reports it.
    """
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal", "user_name": "Ada Lovelace"},
        identity_token=_identity_token(key),
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        yield client, gateway


# ── Starting a sign-in ────────────────────────────────────────────────────


async def test_login_redirects_to_the_gateway_with_a_challenge(client_and_gateway):
    """The request half of PKCE is assembled here, not in a page."""
    client, _ = client_and_gateway

    response = await _begin(client, return_to="/jobs/42")

    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == _AUTHORIZE_URL
    query = parse_qs(target.query)
    assert query["client_id"] == [session_router.OAUTH_CLIENT_ID]
    assert query["redirect_uri"] == [_REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["state"][0]


async def test_the_parked_flow_is_httponly_and_carries_no_verifier_to_the_page(
    client_and_gateway,
):
    """The property that makes this a BFF rather than a page calling an API.

    The verifier used to live in ``localStorage``, where any script on the
    origin could read it — including one an XSS injected, which could then
    complete somebody else's sign-in.
    """
    client, _ = client_and_gateway

    response = await _begin(client)

    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{identity.LOGIN_FLOW_COOKIE}=")
    lowered = raw.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "secure" in lowered
    # The challenge may travel to the gateway; the verifier may not travel
    # anywhere the page can see.
    verifier = identity.verify_login_flow(client.cookies[identity.LOGIN_FLOW_COOKIE])["verifier"]
    assert verifier not in response.headers["location"]
    assert verifier not in response.text


async def test_a_deployment_with_no_gateway_sends_the_visitor_back_with_a_reason(monkeypatch, key):
    """Unconfigured says so — and must not turn the refusal into a loop.

    The app sends an unauthenticated visitor here; if here simply refused,
    the app would send them again, forever. So the refusal travels back as a
    flag the app can render.
    """
    monkeypatch.delenv(session_router.ENV_AUTHORIZE_URL, raising=False)
    gateway = _FakeGateway(identity_token=_identity_token(key))
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _begin(client)

    assert response.status_code == 303
    # `/login`, not the app root: that page is reachable without a session and
    # is the one that can say what is wrong.
    assert response.headers["location"] == (
        f"{_WEB_BASE_URL}/login?{session_router.SIGN_IN_ERROR_PARAM}=unavailable"
    )
    assert not client.cookies.get(identity.LOGIN_FLOW_COOKIE)


async def test_with_nowhere_to_send_them_it_is_a_503(monkeypatch, key):
    """No web base URL means no app to carry the message — say it plainly."""
    monkeypatch.delenv(session_router.ENV_AUTHORIZE_URL, raising=False)
    monkeypatch.delenv(session_router.ENV_WEB_BASE_URL, raising=False)
    gateway = _FakeGateway(identity_token=_identity_token(key))
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _begin(client)

    assert response.status_code == 503


# ── Finishing one ─────────────────────────────────────────────────────────


async def test_sign_in_sets_an_httponly_session_cookie(client_and_gateway):
    """The happy path, and the cookie's attributes are the security surface."""
    client, gateway = client_and_gateway

    response = await _sign_in(client, return_to="/jobs/42")

    assert response.status_code == 303
    assert response.headers["location"] == f"{_WEB_BASE_URL}/jobs/42"
    assert gateway.asked == [_USER]

    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{identity.SESSION_COOKIE}=")
    lowered = raw.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered

    # And what it carries is a session this service can verify, holding the
    # role the *gateway* reported rather than anything the token claimed.
    claims = identity.verify_session(client.cookies[identity.SESSION_COOKIE])
    assert claims["sub"] == _USER
    assert claims["role"] == "internal"


async def test_the_code_is_exchanged_here_and_the_token_never_reaches_the_browser(
    client_and_gateway,
):
    """The regression this whole flow exists to prevent.

    The callback page used to redeem the code itself, which put the gateway's
    identity token — a bearer credential — in page JavaScript. Now the exchange
    happens in this process and the token appears in no response the browser
    ever sees.
    """
    client, gateway = client_and_gateway

    response = await _sign_in(client)

    assert len(gateway.exchanged) == 1
    exchange = gateway.exchanged[0]
    assert exchange["code"] == "the-code"
    assert exchange["redirect_uri"] == _REDIRECT_URI
    assert exchange["verifier"], "the parked verifier is what redeems the code"
    assert gateway.identity_token not in response.text
    assert gateway.identity_token not in str(response.headers)


async def test_a_callback_with_a_mismatched_state_is_refused_before_the_exchange(
    client_and_gateway,
):
    """Login CSRF, and the refusal has to come first or the code is spent."""
    client, gateway = client_and_gateway
    await _begin(client)

    response = await client.get(
        "/v1/session/callback", params={"code": "the-code", "state": "not-the-parked-one"}
    )

    assert response.status_code == 400
    assert gateway.exchanged == [], "a state that did not match must not spend a code"
    assert not client.cookies.get(identity.SESSION_COOKIE)


async def test_a_callback_with_no_parked_flow_is_refused(client_and_gateway):
    """A response to a request this service never made buys nothing."""
    client, gateway = client_and_gateway

    response = await client.get(
        "/v1/session/callback", params={"code": "the-code", "state": "anything"}
    )

    assert response.status_code == 400
    assert gateway.exchanged == []


async def test_a_replayed_callback_finds_nothing_parked(client_and_gateway):
    """The flow is consumed on use, so a code cannot be retried against it."""
    client, gateway = client_and_gateway
    started = await _begin(client)
    state = _state_from(started)
    first = await client.get("/v1/session/callback", params={"code": "the-code", "state": state})
    assert first.status_code == 303

    replay = await client.get("/v1/session/callback", params={"code": "the-code", "state": state})

    assert replay.status_code == 400
    assert len(gateway.exchanged) == 1, "the replay must not reach the gateway"


async def test_a_return_to_pointing_off_site_lands_on_the_app(client_and_gateway):
    """Sign-in must not be usable as an open redirect.

    ``return_to`` survives a round trip through the gateway, so it is
    attacker-influenced: an absolute URL or a protocol-relative one collapses
    to the app's own root.
    """
    client, _ = client_and_gateway

    response = await _sign_in(client, return_to="//evil.example/phish")

    assert response.headers["location"] == f"{_WEB_BASE_URL}/"


async def test_an_account_the_gateway_refuses_at_exchange_is_told_so(key):
    """A 403 at the exchange is a verdict on the account, not a stale code.

    The gateway re-reads status when it redeems the code, precisely so a
    suspension between consent and here takes effect at once. Reporting that
    as "please start again" sends the person round the loop forever — consent,
    callback, refusal — with no sentence anywhere that says why.
    """
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"},
        identity_token=_identity_token(key),
        exchange_error=GatewayError("gateway answered 403 for /v1/identity/token", status_code=403),
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)

    assert response.status_code == 403
    assert response.json()["detail"] == "This account cannot sign in."
    assert not client.cookies.get(identity.SESSION_COOKIE)


async def test_a_refused_exchange_is_a_400_and_an_unreachable_gateway_a_503(key):
    """A stale code is the caller's problem; an outage is the deployment's."""
    for error, expected in (
        (GatewayError("gateway answered 400 for /v1/identity/token", status_code=400), 400),
        (GatewayError("gateway unreachable: nope"), 503),
    ):
        gateway = _FakeGateway(
            {"exists": True, "active": True, "role": "internal"},
            identity_token=_identity_token(key),
            exchange_error=error,
        )
        transport = ASGITransport(app=_app(gateway))
        async with AsyncClient(
            transport=transport, base_url="https://test", follow_redirects=False
        ) as client:
            response = await _sign_in(client)

        assert response.status_code == expected
        assert not client.cookies.get(identity.SESSION_COOKIE)


# ── What the gateway says about the account ───────────────────────────────


async def test_the_gateways_role_wins_over_the_tokens_claim(key):
    """A role in the identity token must not be able to promote its bearer."""
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"},
        identity_token=_identity_token(key, role="admin"),
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        await _sign_in(client)
        claims = identity.verify_session(client.cookies[identity.SESSION_COOKIE])

    assert claims["role"] == "internal"


@pytest.mark.parametrize(
    ("record", "why"),
    [
        ({"exists": True, "active": False, "role": "pro"}, "suspended"),
        ({"exists": False}, "unknown"),
        ({"user_id": _USER, "plan": "whatever-comes-next"}, "unreadable"),
    ],
)
async def test_an_account_the_gateway_will_not_vouch_for_gets_no_cookie(record, why, key):
    """The whole reason the callback asks the gateway at all.

    The identity token is valid — it was minted when this person authenticated
    — and says nothing about a suspension that happened afterwards. An answer
    with no recognizable verdict is not a yes either.
    """
    gateway = _FakeGateway(record, permissions=[], identity_token=_identity_token(key))
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)

    assert response.status_code == 403, why
    assert not client.cookies.get(identity.SESSION_COOKIE)


async def test_an_unreachable_gateway_mints_no_session(key):
    """Fail closed. An outage must not become a way in."""
    gateway = _FakeGateway(error=True, identity_token=_identity_token(key))
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)

    assert response.status_code == 503
    assert not client.cookies.get(identity.SESSION_COOKIE)


async def test_a_token_for_another_audience_is_refused(key):
    """The audience check is what stops one consumer's token acting as ours."""
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"},
        identity_token=_identity_token(key, aud="some-other-service"),
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)

    assert response.status_code == 401
    assert gateway.asked == [], "a token that did not verify must not reach the gateway"


async def test_a_garbage_identity_token_is_a_401(key):
    """Not a 500: an unverifiable credential is an ordinary refusal."""
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"}, identity_token="not-a-jwt"
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)

    assert response.status_code == 401


# ── The session it buys ───────────────────────────────────────────────────


async def test_the_session_is_hours_not_days():
    """Keep identity credentials short-lived even though access is checked afresh."""
    assert identity.SESSION_TTL_S <= 24 * 60 * 60


async def test_me_returns_the_current_user(client_and_gateway):
    """The cookie set at sign-in is the only credential ``/me`` needs."""
    client, _ = client_and_gateway
    await _sign_in(client)

    response = await client.get("/v1/session/me")

    assert response.status_code == 200
    assert response.json()["id"] == _USER
    assert response.json()["role"] == "internal"


async def test_a_display_name_survives_into_me(client_and_gateway_named):
    """When there *is* a name to report, ``/me`` can prove it.

    Nothing on either gateway contract carries a display name today. This is
    what makes the fix the right one: the name is minted into the session, so
    ``/me`` reads it rather than answering ``null`` for the life of the cookie.
    """
    client, _ = client_and_gateway_named
    await _sign_in(client)

    me = await client.get("/v1/session/me")

    assert me.json()["user_name"] == "Ada Lovelace"


async def test_me_is_401_without_a_session(client_and_gateway):
    """ "Not signed in" is the ordinary answer, and must not be a 5xx."""
    client, _ = client_and_gateway

    response = await client.get("/v1/session/me")

    assert response.status_code == 401


async def test_me_is_401_with_a_forged_session(client_and_gateway):
    """A cookie signed with anything else is not a session."""
    client, _ = client_and_gateway
    forged = jwt.encode(
        {"sub": _USER, "role": "admin", "exp": int(time.time()) + 600},
        "some-other-secret-entirely-long-enough",
        algorithm="HS256",
    )
    client.cookies.set(identity.SESSION_COOKIE, forged)

    response = await client.get("/v1/session/me")

    assert response.status_code == 401


async def test_a_login_flow_cookie_is_not_a_session(client_and_gateway):
    """Two things signed under one secret must not be interchangeable.

    Both are HS256 under ``AGENT_SESSION_SECRET``. They are separated by a
    derived key rather than by a claim someone has to remember to check, so a
    parked sign-in presented as a session is refused by the signature itself.
    """
    client, _ = client_and_gateway
    started = await _begin(client)
    parked = client.cookies[identity.LOGIN_FLOW_COOKIE]
    assert started.status_code == 303
    client.cookies.set(identity.SESSION_COOKIE, parked)

    response = await client.get("/v1/session/me")

    assert response.status_code == 401


async def test_me_is_401_when_the_session_has_expired(client_and_gateway):
    """And says so, so a client knows to sign in rather than to retry."""
    client, _ = client_and_gateway
    expired = jwt.encode(
        {"sub": _USER, "role": "internal", "exp": int(time.time()) - 1},
        _SESSION_SECRET,
        algorithm="HS256",
    )
    client.cookies.set(identity.SESSION_COOKIE, expired)

    response = await client.get("/v1/session/me")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Token has expired")


async def test_logout_clears_the_cookie(client_and_gateway):
    """And works without one, because signing out must not need a session."""
    client, _ = client_and_gateway
    await _sign_in(client)

    response = await client.post("/v1/session/logout")

    assert response.status_code == 200
    assert not client.cookies.get(identity.SESSION_COOKIE)
    assert (await client.get("/v1/session/me")).status_code == 401
    # Idempotent: a second logout with no session is still a 200.
    assert (await client.post("/v1/session/logout")).status_code == 200


async def test_a_deployment_that_cannot_verify_sessions_says_503(monkeypatch, key):
    """Unset secret is an operator problem, reported as one — never a pass."""
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"},
        identity_token=_identity_token(key),
    )
    transport = ASGITransport(app=_app(gateway))
    monkeypatch.delenv(identity.ENV_SESSION_SECRET, raising=False)
    async with AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as client:
        response = await _begin(client)

    assert response.status_code == 503


async def test_secure_can_be_turned_off_only_deliberately(monkeypatch, key):
    """The dev-only escape hatch, pinned so it stays opt-*out*."""
    monkeypatch.setenv(session_router.ENV_COOKIE_SECURE, "false")
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "internal"},
        identity_token=_identity_token(key),
    )
    transport = ASGITransport(app=_app(gateway))
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)

    assert "secure" not in response.headers["set-cookie"].lower()


async def test_login_denies_an_admin_profile_without_agent_access(key):
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "admin"},
        permissions=[],
        identity_token=_identity_token(key, role="admin"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)
        assert response.status_code == 403
        assert not client.cookies.get(identity.SESSION_COOKIE)
    assert gateway.access_asked == [_USER]
    assert gateway.asked == [], "profile metadata must not substitute for denied admission"


async def test_login_accepts_a_free_profile_with_explicit_agent_access(key):
    gateway = _FakeGateway(
        {"exists": True, "active": True, "role": "free"}, identity_token=_identity_token(key)
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test", follow_redirects=False
    ) as client:
        response = await _sign_in(client)
        assert response.status_code == 303
        claims = identity.verify_session(client.cookies[identity.SESSION_COOKIE])
        assert claims["role"] == "free"
        assert "permissions" not in claims
        assert (await client.get("/v1/session/me")).json()["agent_access"] == {
            "allowed": True,
            "permissions": ["agent.use"],
        }


async def test_me_reports_revocation_without_erasing_authenticated_identity(client_and_gateway):
    client, gateway = client_and_gateway
    await _sign_in(client)
    assert (await client.get("/v1/session/me")).json()["agent_access"]["allowed"] is True
    gateway.permissions = []
    response = await client.get("/v1/session/me")
    assert response.status_code == 200
    assert response.json()["id"] == _USER
    assert response.json()["agent_access"] == {"allowed": False, "permissions": []}
    assert gateway.access_asked == [_USER, _USER, _USER]


async def test_me_fails_closed_after_an_earlier_allow_when_gateway_is_unavailable(
    client_and_gateway,
):
    client, gateway = client_and_gateway
    await _sign_in(client)
    gateway.error = True
    response = await client.get("/v1/session/me")
    assert response.status_code == 503
    assert "agent_access" not in response.json()
