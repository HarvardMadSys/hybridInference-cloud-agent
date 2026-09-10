"""The auth dependencies E7 replaced (``cloud_agent.api.deps``).

Until E7 these eight raised ``NotImplementedError`` and the ported API tests
supplied stand-ins. They are real now, and this file is what stops them
regressing into something permissive: nearly every case here asserts a
*refusal*, because the failure mode that matters is a dependency that says yes
when it cannot know.

The credential boundaries that would be quietest if they broke:

- an **unset** ``ADMIN_TOKEN`` must open nothing, rather than matching the
  empty string a caller can trivially present;
- ``X-Forwarded-For`` must be **ignored by default**, or the audit trail
  records whatever the caller typed.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.api import deps
from cloud_agent.api.deps import (
    AGENT_TOKEN_PREFIX,
    get_client_ip,
    get_current_user,
    log_admin_action,
    looks_like_agent_token,
    verify_admin_access,
)

pytestmark = pytest.mark.asyncio

_SESSION_SECRET = "session-signing-secret-long-enough-for-hs256"
_USER = "user_1"


@pytest.fixture(autouse=True)
def _session_env(monkeypatch):
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, _SESSION_SECRET)
    monkeypatch.delenv(deps.ENV_ADMIN_TOKEN, raising=False)
    monkeypatch.delenv(deps.ENV_TRUST_PROXY_HEADERS, raising=False)
    yield


def _session(*, role: str = "internal", expired: bool = False, email: str | None = None) -> str:
    """Mint what ``/v1/session/login`` would have set."""
    token = identity.mint_session(user_id=_USER)
    if not expired:
        return token
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = int(time.time()) - 1
    return jwt.encode(claims, _SESSION_SECRET, algorithm="HS256")


@pytest_asyncio.fixture()
async def client():
    """An app exposing the two request-scoped dependencies directly."""
    app = FastAPI()

    class Gateway:
        async def agent_access(self, user_id):
            return {
                "user_id": user_id,
                "allowed": True,
                "permissions": ["agent.use", "agent.admin"],
            }

    app.state.gateway = Gateway()

    @app.get("/who")
    async def who(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        return user

    @app.get("/admin")
    async def admin(admin_id: str = Depends(verify_admin_access)) -> dict[str, str]:
        return {"admin_id": admin_id}

    @app.get("/ip")
    async def ip(request: Request) -> dict[str, str]:
        return {"ip": get_client_ip(request)}

    transport = ASGITransport(app=app, client=("203.0.113.9", 51234))
    async with AsyncClient(transport=transport, base_url="https://test") as async_client:
        yield async_client


# ── Credential namespaces ───────────────────────────────────────────────


async def test_a_worker_token_is_recognized_as_one() -> None:
    assert looks_like_agent_token("ajt.abc.def") is True
    assert looks_like_agent_token("hyi-normal-api-key") is False
    assert looks_like_agent_token("agr.grant.token") is False
    assert looks_like_agent_token(None) is False
    assert looks_like_agent_token("") is False


async def test_the_agent_token_prefix_still_matches_what_tokens_mints(monkeypatch) -> None:
    """The prefix is a literal here and private there; this is the tripwire.

    If the two ever drift, a worker token stops being recognized as one and
    starts being tried as an API key — which is the exact confusion the
    namespace check exists to prevent.
    """
    from cloud_agent.tokens import mint_worker_token

    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-secret")
    minted = mint_worker_token(job_id="ajob_1", attempt_id=1, lease_generation=1)

    assert minted.startswith(AGENT_TOKEN_PREFIX)
    assert looks_like_agent_token(minted) is True


# ── get_current_user ───────────────────────────────────────────────────


async def test_the_session_cookie_authenticates(client: AsyncClient) -> None:
    client.cookies.set(identity.SESSION_COOKIE, _session(email="owner@example.com"))

    response = await client.get("/who")

    assert response.status_code == 200
    assert response.json() == {"user_id": _USER, "authenticated": True}


async def test_a_bearer_session_authenticates_too(client: AsyncClient) -> None:
    """The programmatic caller holding a session is not turned away.

    It is the same credential; the cookie is only how a browser carries it.
    """
    response = await client.get("/who", headers={"Authorization": f"Bearer {_session()}"})

    assert response.status_code == 200
    assert response.json()["user_id"] == _USER


async def test_no_credential_is_a_401(client: AsyncClient) -> None:
    response = await client.get("/who")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Missing authentication token")


async def test_an_expired_session_says_so(client: AsyncClient) -> None:
    """The message is load-bearing: it is how a client knows to sign in again
    rather than to retry the same credential."""
    client.cookies.set(identity.SESSION_COOKIE, _session(expired=True))

    response = await client.get("/who")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Token has expired")


async def test_a_session_signed_with_another_secret_is_refused(client: AsyncClient) -> None:
    forged = jwt.encode(
        {"sub": _USER, "role": "admin", "exp": int(time.time()) + 600},
        "a-completely-different-secret-of-sufficient-length",
        algorithm="HS256",
    )
    client.cookies.set(identity.SESSION_COOKIE, forged)

    response = await client.get("/who")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid authentication token."


async def test_a_deployment_with_no_session_secret_says_503(
    client: AsyncClient, monkeypatch
) -> None:
    """Not 401. The caller did nothing wrong and cannot fix it by retrying."""
    client.cookies.set(identity.SESSION_COOKIE, _session())
    monkeypatch.delenv(identity.ENV_SESSION_SECRET, raising=False)

    response = await client.get("/who")

    assert response.status_code == 503


async def test_the_identity_context_contains_no_profile(
    client: AsyncClient,
) -> None:
    """Downstream owner handlers receive only the verified user identifier."""
    client.cookies.set(
        identity.SESSION_COOKIE,
        identity.mint_session(user_id=_USER),
    )

    response = await client.get("/who")

    assert response.json() == {"user_id": _USER, "authenticated": True}


async def test_a_worker_token_is_not_read_as_a_session(client: AsyncClient) -> None:
    """Namespaces are selected before validation, not after a failure."""
    response = await client.get("/who", headers={"Authorization": "Bearer ajt.payload.signature"})

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Missing authentication token")


# ── verify_admin_access ────────────────────────────────────────────────


async def test_the_admin_token_opens_the_admin_door(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv(deps.ENV_ADMIN_TOKEN, "the-admin-token")

    response = await client.get("/admin", headers={"Authorization": "Bearer the-admin-token"})

    assert response.status_code == 200
    assert response.json()["admin_id"] == "admin-token"


async def test_an_unset_admin_token_opens_nothing(client: AsyncClient) -> None:
    """The quiet one. A deployment that forgot to configure ``ADMIN_TOKEN``
    must not be an open admin surface, and an empty configured value must not
    match an empty presented one."""
    for header in ({"Authorization": "Bearer "}, {"Authorization": "Bearer anything"}, {}):
        response = await client.get("/admin", headers=header)
        assert response.status_code == 401, header


async def test_a_wrong_admin_token_is_refused(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv(deps.ENV_ADMIN_TOKEN, "the-admin-token")

    response = await client.get("/admin", headers={"Authorization": "Bearer nearly-the-token"})

    assert response.status_code == 401


async def test_an_admin_session_opens_the_admin_door(client: AsyncClient) -> None:
    """The dashboard uses a session and the Gateway's current agent.admin grant."""
    client.cookies.set(identity.SESSION_COOKIE, _session(role="admin"))

    response = await client.get("/admin")

    assert response.status_code == 200
    assert response.json()["admin_id"] == f"admin:{_USER}"


@pytest.mark.parametrize("role", ["free", "pro", "internal"])
async def test_an_agent_admin_permission_works_for_every_profile_role(
    client: AsyncClient, role: str
) -> None:
    client.cookies.set(identity.SESSION_COOKIE, _session(role=role))

    response = await client.get("/admin")

    assert response.status_code == 200


async def test_the_admin_door_diagnoses_a_missing_secret_the_same_way(
    client: AsyncClient, monkeypatch
) -> None:
    """One misconfiguration, one diagnosis.

    This used to answer 401 while ``get_current_user`` answered 503 for the
    identical cause — an unset ``AGENT_SESSION_SECRET`` — because the broader
    ``IdentityError`` was caught here. The 401 sent an operator hunting for a
    credential problem that did not exist.
    """
    client.cookies.set(identity.SESSION_COOKIE, _session(role="admin"))
    monkeypatch.delenv(identity.ENV_SESSION_SECRET, raising=False)

    response = await client.get("/admin")

    assert response.status_code == 503


async def test_the_admin_token_still_opens_without_a_session_secret(
    client: AsyncClient, monkeypatch
) -> None:
    """A deployment that authenticates machines only keeps working.

    The token is compared before any session is verified, so 503 is the answer
    for callers who need session verification — not for the one door that does
    not.
    """
    monkeypatch.setenv(deps.ENV_ADMIN_TOKEN, "the-admin-token")
    monkeypatch.delenv(identity.ENV_SESSION_SECRET, raising=False)

    response = await client.get("/admin", headers={"Authorization": "Bearer the-admin-token"})

    assert response.status_code == 200


# ── get_client_ip ──────────────────────────────────────────────────────


async def test_the_forwarded_header_is_ignored_by_default(client: AsyncClient) -> None:
    """Otherwise the audit trail records whatever the caller typed."""
    response = await client.get("/ip", headers={"X-Forwarded-For": "198.51.100.7"})

    assert response.json()["ip"] == "203.0.113.9"


async def test_the_forwarded_header_is_read_when_trusted(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv(deps.ENV_TRUST_PROXY_HEADERS, "true")

    response = await client.get("/ip", headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})

    assert response.json()["ip"] == "198.51.100.7"


async def test_a_trusted_deployment_falls_back_to_the_socket(
    client: AsyncClient, monkeypatch
) -> None:
    """A request that arrived without the header still records an address."""
    monkeypatch.setenv(deps.ENV_TRUST_PROXY_HEADERS, "true")

    response = await client.get("/ip")

    assert response.json()["ip"] == "203.0.113.9"


# ── log_admin_action ───────────────────────────────────────────────────


class _RecordingStore:
    def __init__(self, explode: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self.explode = explode

    async def record_admin_action(self, **kwargs: Any) -> int:
        if self.explode:
            raise RuntimeError("database went away")
        self.rows.append(kwargs)
        return len(self.rows)


async def test_an_admin_action_reaches_the_trail() -> None:
    store = _RecordingStore()

    await log_admin_action(
        store,
        "203.0.113.9",
        "agent.runner_host.drain",
        None,
        {"host": "runner-a"},
        actor_id="admin-1",
    )

    assert store.rows == [
        {
            "action": "agent.runner_host.drain",
            "actor_id": "admin-1",
            "admin_ip": "203.0.113.9",
            "target_user_id": None,
            "details": {"host": "runner-a"},
            "success": True,
        }
    ]


async def test_no_store_is_a_silent_no_op() -> None:
    """A deployment with no database still serves; it just records nothing."""
    await log_admin_action(None, "203.0.113.9", "agent.runner_host.drain", actor_id="admin-1")


async def test_a_store_without_the_method_is_a_no_op() -> None:
    """The fakes in the ported host tests are exactly this shape."""

    class _Old:
        pass

    await log_admin_action(_Old(), "203.0.113.9", "agent.runner_host.drain", actor_id="admin-1")


async def test_a_failed_audit_write_never_undoes_the_action() -> None:
    """The drain already happened. Raising here would report it as failed."""
    await log_admin_action(_RecordingStore(explode=True), "203.0.113.9", "x.y", actor_id="admin-1")


@pytest.fixture(autouse=True)
def _configured_identity_issuer(monkeypatch):
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
