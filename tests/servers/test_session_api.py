"""The session routes that remain: who a browser is, and signing it out.

Sign-in lives under ``/v1/auth``. These two reads are shared by every page, so
what they refuse matters: an unknown or ended session is 401, a withdrawn
account stays identifiable with a denied decision, and an unreadable store is
503 rather than a stale answer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.owners import FakeOwners, SessionAccounts, use_session_secret

from cloud_agent.accounts.config import (
    LOCAL_SESSION_COOKIE,
    LOCAL_SESSION_COOKIE_INSECURE,
)
from cloud_agent.api import session as session_router
from cloud_agent.api.deps import get_owner_directory, get_storage

pytestmark = pytest.mark.asyncio

_USER = "01J0000000000000000000USER"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    use_session_secret(monkeypatch)
    monkeypatch.delenv("AGENT_SESSION_COOKIE_SECURE", raising=False)


@pytest.fixture
def accounts() -> SessionAccounts:
    return SessionAccounts()


@pytest.fixture
def owners() -> FakeOwners:
    return FakeOwners()


@pytest_asyncio.fixture()
async def client(accounts: SessionAccounts, owners: FakeOwners):
    app = FastAPI()
    app.include_router(session_router.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(local_accounts=accounts)
    app.dependency_overrides[get_owner_directory] = lambda: owners
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as async_client:
        yield async_client


async def test_me_without_a_session_is_a_401(client: AsyncClient):
    assert (await client.get("/v1/session/me")).status_code == 401


async def test_me_answers_the_account_and_its_current_access(
    client: AsyncClient, accounts: SessionAccounts
):
    client.cookies.set(LOCAL_SESSION_COOKIE, accounts.sign_in(_USER))
    response = await client.get("/v1/session/me")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == _USER
    assert body["agent_access"]["allowed"] is True
    assert "auth_mode" not in body


async def test_me_reads_access_afresh_so_a_withdrawal_shows_at_once(
    client: AsyncClient, accounts: SessionAccounts, owners: FakeOwners
):
    client.cookies.set(LOCAL_SESSION_COOKIE, accounts.sign_in(_USER))
    assert (await client.get("/v1/session/me")).json()["agent_access"]["allowed"] is True
    owners.grant(_USER)
    denied = await client.get("/v1/session/me")
    assert denied.status_code == 200, "a withdrawn account stays identifiable"
    assert denied.json()["agent_access"]["allowed"] is False


async def test_me_says_503_when_the_account_cannot_be_read(
    client: AsyncClient, accounts: SessionAccounts, owners: FakeOwners
):
    client.cookies.set(LOCAL_SESSION_COOKIE, accounts.sign_in(_USER))
    owners.outage = True
    assert (await client.get("/v1/session/me")).status_code == 503


async def test_an_ended_session_is_a_401(client: AsyncClient, accounts: SessionAccounts):
    client.cookies.set(LOCAL_SESSION_COOKIE, accounts.sign_in(_USER))
    accounts.revoke_all()
    assert (await client.get("/v1/session/me")).status_code == 401


async def test_the_retired_gateway_cookie_is_not_read(
    client: AsyncClient, accounts: SessionAccounts
):
    """Even holding a live session's value, that name authenticates nothing."""
    client.cookies.set("__Host-cloud_agent_session", accounts.sign_in(_USER))
    assert (await client.get("/v1/session/me")).status_code == 401


async def test_the_plain_cookie_name_is_not_read_where_the_cookie_is_secure(
    client: AsyncClient, accounts: SessionAccounts
):
    """A sibling host can plant the plain name; only ``__Host-`` counts over HTTPS."""
    client.cookies.set(LOCAL_SESSION_COOKIE_INSECURE, accounts.sign_in(_USER))
    assert (await client.get("/v1/session/me")).status_code == 401


async def test_sign_out_with_no_session_still_clears_the_cookie(client: AsyncClient):
    response = await client.post("/v1/session/logout")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert any(
        header.startswith(f"{LOCAL_SESSION_COOKIE}=")
        for header in response.headers.get_list("set-cookie")
    )


async def test_a_malformed_cookie_needs_no_store_to_sign_out(client: AsyncClient):
    client.cookies.set(LOCAL_SESSION_COOKIE, "not-a-session")
    assert (await client.post("/v1/session/logout")).status_code == 200


@pytest.mark.parametrize("path", ["/v1/session/login", "/v1/session/callback"])
async def test_gateway_sign_in_routes_are_gone(client: AsyncClient, path: str):
    assert (await client.get(path)).status_code == 404
