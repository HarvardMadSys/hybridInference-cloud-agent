"""How sign-in attempts are counted, without a database.

The counters are an in-memory stand-in for ``hit_rate_limit`` that counts the
way the Postgres one does: every call adds one, refused or not. What is under
test is which windows an attempt reaches: both per-address windows always,
and the account's own window only after both let the attempt through, so one
address cannot lock an account's owner out of every other address.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from cloud_agent.accounts import config
from cloud_agent.accounts.tokens import rate_limit_key
from cloud_agent.api import auth
from cloud_agent.api.deps import ENV_TRUST_PROXY_HEADERS

pytestmark = pytest.mark.asyncio

_OWNER = "owner@example.org"
_OWNER_ADDRESS = "203.0.113.200"


class _Counters:
    """``hit_rate_limit`` over a dict: one more hit per call, over once past ``limit``."""

    def __init__(self) -> None:
        self.hits: Counter[tuple[str, bytes]] = Counter()
        self.calls: list[str] = []
        self.fail_bucket: str | None = None

    async def hit_rate_limit(
        self, *, bucket: str, key_hash: bytes, window_s: int, limit: int
    ) -> float:
        self.calls.append(bucket)
        if bucket == self.fail_bucket:
            raise ConnectionError("database away")
        self.hits[(bucket, key_hash)] += 1
        return 0.0 if self.hits[(bucket, key_hash)] <= limit else float(window_s)

    def count(self, bucket: str, value: str) -> int:
        return self.hits[(bucket, rate_limit_key(bucket, value))]


@pytest.fixture()
def counters(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, "local-session-secret-for-tests-0123456789")
    monkeypatch.delenv(ENV_TRUST_PROXY_HEADERS, raising=False)
    return _Counters()


def _request(counters: _Counters, address: str) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(storage=SimpleNamespace(local_accounts=counters)))
    return Request({"type": "http", "headers": [], "client": (address, 40000), "app": app})


async def _attempt(counters: _Counters, email: str, address: str) -> int:
    """The status the limits answer: 200 if the attempt goes on to the password check."""
    try:
        await auth.enforce_login_limits(_request(counters, address), email)
    except HTTPException as refused:
        return refused.status_code
    return 200


async def test_one_address_over_its_limit_never_reaches_the_account_window(counters):
    statuses = [await _attempt(counters, _OWNER, "198.51.100.7") for _ in range(100)]
    assert statuses == [200] * 5 + [429] * 95
    # Every attempt counted per address; only the five let through per account.
    assert counters.count("login_email_ip", f"{_OWNER}\x00198.51.100.7") == 100
    assert counters.count("login_ip", "198.51.100.7") == 100
    assert counters.count("login_email", _OWNER) == 5
    assert counters.calls.count("login_email") == 5
    # The owner, anywhere else, still reaches the password check.
    assert await _attempt(counters, _OWNER, _OWNER_ADDRESS) == 200


async def test_an_address_over_its_hourly_limit_adds_nothing_to_any_account(counters):
    for index in range(20):
        assert await _attempt(counters, f"user{index}@example.org", "198.51.100.8") == 200
    assert await _attempt(counters, _OWNER, "198.51.100.8") == 429
    assert counters.count("login_email_ip", f"{_OWNER}\x00198.51.100.8") == 1
    assert counters.count("login_email", _OWNER) == 0
    assert await _attempt(counters, _OWNER, _OWNER_ADDRESS) == 200


async def _flood(counters: _Counters, addresses: int) -> list[list[int]]:
    """Each address tries well past its own allowance."""
    return [
        [await _attempt(counters, _OWNER, f"198.51.100.{index}") for _ in range(8)]
        for index in range(1, addresses + 1)
    ]


async def test_nine_addresses_do_not_fill_the_account_window(counters):
    assert await _flood(counters, 9) == [[200] * 5 + [429] * 3] * 9
    assert counters.count("login_email", _OWNER) == 45
    assert await _attempt(counters, _OWNER, _OWNER_ADDRESS) == 200


async def test_ten_addresses_are_what_fills_the_account_window(counters):
    assert await _flood(counters, 10) == [[200] * 5 + [429] * 3] * 10
    assert counters.count("login_email", _OWNER) == 50
    assert await _attempt(counters, _OWNER, _OWNER_ADDRESS) == 429


@pytest.mark.parametrize(
    "spelling",
    [
        "OWNER@EXAMPLE.ORG",
        "  owner@example.org\t",
        "owner@\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.org",
        "Owner@Example.Org",
    ],
)
async def test_every_spelling_of_one_address_shares_its_windows(counters, spelling):
    for _ in range(5):
        assert await _attempt(counters, _OWNER, "198.51.100.9") == 200
    assert await _attempt(counters, spelling, "198.51.100.9") == 429
    assert counters.count("login_email", _OWNER) == 5


async def test_unicode_spellings_of_one_mailbox_share_its_windows(counters):
    composed = "jos\u00e9@ex\u00e4mple.org"
    spellings = [
        composed,
        "jose\u0301@exa\u0308mple.org",
        "jos\u00e9@xn--exmple-cua.org",
        "JOS\u00c9@EX\u00c4MPLE.ORG",
        "jos\u00e9@EXA\u0308MPLE.org",
    ]
    assert [await _attempt(counters, spelling, "198.51.100.9") for spelling in spellings] == [
        200
    ] * 5
    assert await _attempt(counters, composed, "198.51.100.9") == 429
    assert counters.count("login_email", composed) == 5


@pytest.mark.parametrize(
    "malformed", ["not-an-email", "owner@example.org.", '"owner"@example.org', "owner@\u200b"]
)
async def test_a_malformed_address_is_counted_apart_from_every_real_one(counters, malformed):
    statuses = [await _attempt(counters, malformed, "198.51.100.10") for _ in range(6)]
    assert statuses == [200] * 5 + [429]
    raw = f"raw:{malformed.strip().lower()}"
    assert counters.count("login_email_ip", f"{raw}\x00198.51.100.10") == 6
    assert counters.count("login_email", raw) == 5
    # It spends nothing of the real account's windows.
    assert counters.count("login_email", _OWNER) == 0
    assert await _attempt(counters, _OWNER, "198.51.100.10") == 200


async def test_an_address_that_cannot_be_counted_is_refused(counters):
    counters.fail_bucket = "login_email"
    assert await _attempt(counters, _OWNER, "198.51.100.11") == 503
    counters.fail_bucket = "login_email_ip"
    assert await _attempt(counters, _OWNER, "198.51.100.11") == 503
    assert counters.calls[-1] == "login_email_ip"


async def test_a_refusal_waits_for_the_window_that_refused(counters):
    for _ in range(5):
        await _attempt(counters, _OWNER, "198.51.100.12")
    with pytest.raises(HTTPException) as refused:
        await auth.enforce_login_limits(_request(counters, "198.51.100.12"), _OWNER)
    assert refused.value.headers["Retry-After"] == str(15 * 60)
