"""The owner credential dispatch (``routes.authenticate_agent_owner``).

The dispatch used to choose between two authenticators: this service's session,
and the gateway's ``api_keys`` table. **The API-key branch was deleted on
2026-08-04** — it had no producer here and never will, so it answered 503 in
every real deployment while looking implemented. See MIGRATION.md's deferred
register for the ruling and its discarded-test row for the ported case that
went with it.

This file is that case's replacement, and it pins the two things the deletion
made possible to get wrong:

- **An ``hyi-*`` key authenticates nobody, and says so as 401** — not 503. A
  reinstated branch with no store behind it answers 503, so this fails the day
  anyone brings it back without also bringing a gateway contract.
- **A worker capability token is still refused as 403 ``insufficient_scope``**,
  which ``verify_api_key`` used to own. That refusal is the sandbox-credential
  guarantee; it had to survive the function it lived in.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.api.routes import authenticate_agent_owner

pytestmark = pytest.mark.asyncio

_SESSION_SECRET = "session-signing-secret-long-enough-for-hs256"
_USER = "user_1"
_API_KEY = "hyi-programmatic-client"


@pytest.fixture(autouse=True)
def _session_env(monkeypatch):
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, _SESSION_SECRET)
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-control-secret")
    yield


@pytest_asyncio.fixture()
async def client():
    """An app exposing the dispatch directly, with no store of any kind."""
    app = FastAPI()

    @app.get("/owner")
    async def owner(user: dict[str, Any] = Depends(authenticate_agent_owner)) -> dict[str, Any]:
        return user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as async_client:
        yield async_client


def _worker_token() -> str:
    from cloud_agent.tokens import SCOPE_MODEL, mint_worker_token

    return mint_worker_token(
        job_id="ajob_0001", attempt_id=1, lease_generation=1, scope=SCOPE_MODEL
    )


async def test_the_session_cookie_authenticates_the_owner(client: AsyncClient) -> None:
    """The one owner credential this service has."""
    client.cookies.set(
        identity.SESSION_COOKIE,
        identity.mint_session(user_id=_USER, email="owner@example.com", role="admin"),
    )

    response = await client.get("/owner")

    assert response.status_code == 200
    assert response.json()["user_id"] == _USER


async def test_a_session_bearer_authenticates_the_owner(client: AsyncClient) -> None:
    """Same credential, carried the way a programmatic caller carries it."""
    token = identity.mint_session(user_id=_USER, email=None, role="admin")

    response = await client.get("/owner", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["user_id"] == _USER


async def test_an_api_key_authenticates_nobody(client: AsyncClient) -> None:
    """The tripwire. 401, and specifically **not** 503.

    503 is what a reinstated API-key branch answers when no store is
    configured — which is every deployment, since the gateway kept ``api_keys``
    and ``GatewayClient`` has no method that resolves a key to a user. So this
    assertion fails the day someone restores the branch without also adding the
    contract that would make it work.
    """
    response = await client.get("/owner", headers={"Authorization": f"Bearer {_API_KEY}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid authentication token."


async def test_the_x_api_key_header_is_not_read_at_all(client: AsyncClient) -> None:
    """It was the API-key branch's second input. Nothing reads it now."""
    response = await client.get("/owner", headers={"X-API-Key": _API_KEY})

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Missing authentication token")


async def test_a_worker_token_is_refused_as_insufficient_scope(client: AsyncClient) -> None:
    """``verify_api_key`` owned this refusal; it had to outlive it.

    The sandbox's credential must be told apart from an unknown one — an
    escaped worker token is a different incident from a stale login, and 403
    ``insufficient_scope`` is what says so.
    """
    response = await client.get("/owner", headers={"Authorization": f"Bearer {_worker_token()}"})

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"


async def test_the_cookie_wins_when_a_worker_bearer_rides_along(client: AsyncClient) -> None:
    """Precedence is unchanged: ``session_token`` prefers the cookie.

    Before the deletion the cookie was checked first and the bearer never
    reached the API-key branch. Keeping that ordering means the only requests
    whose answer changed are the ones that used to reach a store.
    """
    client.cookies.set(
        identity.SESSION_COOKIE,
        identity.mint_session(user_id=_USER, email=None, role="admin"),
    )

    response = await client.get("/owner", headers={"Authorization": f"Bearer {_worker_token()}"})

    assert response.status_code == 200
    assert response.json()["user_id"] == _USER
