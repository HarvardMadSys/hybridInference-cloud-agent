"""Sign-in limits over HTTP and real Postgres: one address cannot lock an owner out.

Each request names its client address in ``X-Forwarded-For``, which this
deployment is told to believe, so one client plays many addresses. The
counters are read straight from ``agent_local_auth_rate_limits``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.support.local_accounts import LOCAL_SECRET, PASSWORD, close_pool, make_account, open_pool

from cloud_agent.accounts import config
from cloud_agent.accounts.directory import (
    LocalModeGateway,
    LocalModeGrantClient,
    LocalOwnerDirectory,
)
from cloud_agent.accounts.tokens import rate_limit_key
from cloud_agent.api.deps import ENV_TRUST_PROXY_HEADERS
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_EMAIL = "owner@example.org"
_OWNER_ADDRESS = "203.0.113.200"
_WRONG = "Wrong1Horse"


@pytest_asyncio.fixture
async def pool():
    created = await open_pool()
    try:
        yield created
    finally:
        await close_pool(created)


@pytest_asyncio.fixture
async def http(pool, monkeypatch):
    monkeypatch.setenv(config.ENV_AUTH_MODE, "local")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    monkeypatch.setenv(ENV_TRUST_PROXY_HEADERS, "1")
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    from cloud_agent.app import create_app

    app = create_app()
    storage = Storage(pool)
    app.state.storage = storage
    app.state.owners = LocalModeGateway(LocalOwnerDirectory(storage.local_accounts))
    app.state.grants = LocalModeGrantClient()
    await make_account(storage, _EMAIL)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test", follow_redirects=False
    ) as client:
        yield client


async def _login(http, address: str, password: str = PASSWORD, email: str = _EMAIL) -> int:
    http.cookies.clear()
    response = await http.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
        headers={"x-forwarded-for": address},
    )
    return response.status_code


async def _hits(pool, bucket: str, value: str) -> int:
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(hits), 0) FROM agent_local_auth_rate_limits "
            "WHERE bucket = $1 AND key_hash = $2",
            bucket,
            rate_limit_key(bucket, value),
        )
    return int(total)


async def test_one_address_cannot_lock_the_owner_out(http, pool):
    attacker = "198.51.100.7"
    statuses = [await _login(http, attacker, _WRONG) for _ in range(60)]
    assert statuses == [401] * 5 + [429] * 55
    assert await _hits(pool, "login_email_ip", f"{_EMAIL}\x00{attacker}") == 60
    # Only the five attempts the address was allowed reached the account's window.
    assert await _hits(pool, "login_email", _EMAIL) == 5
    # The right password from the attacker's address is refused like a wrong one...
    assert await _login(http, attacker) == 429
    # ...and the owner, elsewhere, signs in.
    assert await _login(http, _OWNER_ADDRESS) == 200


async def test_an_address_over_its_hourly_limit_adds_nothing_to_the_account(http, pool):
    stuffer = "198.51.100.8"
    for index in range(20):
        assert await _login(http, stuffer, _WRONG, email=f"user{index}@example.org") == 401
    assert await _login(http, stuffer, _WRONG) == 429
    assert await _hits(pool, "login_ip", stuffer) == 21
    assert await _hits(pool, "login_email", _EMAIL) == 0
    assert await _login(http, _OWNER_ADDRESS) == 200


async def test_ten_addresses_fill_the_account_window(http, pool):
    for index in range(1, 10):
        address = f"198.51.100.{index}"
        assert [await _login(http, address, _WRONG) for _ in range(7)] == [401] * 5 + [429] * 2
    assert await _hits(pool, "login_email", _EMAIL) == 45
    # Nine addresses are not enough: a tenth still has its five attempts.
    tenth = [await _login(http, "198.51.100.10", _WRONG) for _ in range(7)]
    assert tenth == [401] * 5 + [429] * 2
    assert await _hits(pool, "login_email", _EMAIL) == 50
    # Ten are: the account's window is full for every address, the owner's too.
    assert await _login(http, _OWNER_ADDRESS) == 429


async def test_a_successful_sign_in_counts_like_any_other_attempt(http, pool):
    assert [await _login(http, _OWNER_ADDRESS) for _ in range(5)] == [200] * 5
    assert await _login(http, _OWNER_ADDRESS) == 429
    assert await _hits(pool, "login_email_ip", f"{_EMAIL}\x00{_OWNER_ADDRESS}") == 6
    assert await _hits(pool, "login_email", _EMAIL) == 5


async def test_spellings_of_the_address_share_its_windows(http, pool):
    address = "198.51.100.9"
    spellings = [
        "OWNER@EXAMPLE.ORG",
        "  owner@example.org\t",
        "owner@\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.org",
        "Owner@Example.Org",
        _EMAIL,
    ]
    assert [await _login(http, address, _WRONG, email=s) for s in spellings] == [401] * 5
    assert await _login(http, address, email=spellings[2]) == 429
    async with pool.acquire() as conn:
        rows = await conn.fetchval(
            "SELECT count(*) FROM agent_local_auth_rate_limits WHERE bucket = 'login_email'"
        )
    assert rows == 1
    assert await _hits(pool, "login_email", _EMAIL) == 5


async def test_a_malformed_address_is_counted_and_spends_nothing_of_the_account(http, pool):
    address = "198.51.100.10"
    malformed = f"{_EMAIL}."
    statuses = [await _login(http, address, _WRONG, email=malformed) for _ in range(6)]
    assert statuses == [401] * 5 + [429]
    assert await _hits(pool, "login_email_ip", f"raw:{malformed}\x00{address}") == 6
    assert await _hits(pool, "login_email", _EMAIL) == 0
    assert await _login(http, address) == 200
