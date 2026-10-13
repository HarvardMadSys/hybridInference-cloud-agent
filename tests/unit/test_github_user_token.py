"""Keeping a user's GitHub token usable: when it is renewed, and by whom.

A GitHub App's refresh token works once. Two callers renewing the same token
would spend it twice, and GitHub would refuse the second — destroying the
connection both were keeping. These pin the lease that prevents that, and what
each caller does when it does not hold it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

import cloud_agent.github_app as github_app
import cloud_agent.github_user_token as github_user_token
from cloud_agent.github_app import (
    AppConfig,
    GitHubAppCredentials,
    UserAuthorizationRevoked,
    UserTokens,
)
from cloud_agent.github_user_token import UserTokenUnavailable, user_access_token
from cloud_agent.source_control import SourceControlCipher

_USER = "user-owner"


class _App:
    """The App's refresh endpoint, counted."""

    def __init__(self, *, refused: bool = False, request_timeout_s: float = 30.0) -> None:
        self.cipher = SourceControlCipher("test-source-control-key")
        self.refused = refused
        self.request_timeout_s = request_timeout_s
        self.refreshed_with: list[str] = []
        self.refresh_timeouts: list[float] = []
        self.refresh_started = asyncio.Event()
        self.refresh_gate: asyncio.Event | None = None

    async def refresh_user_token(self, refresh_token: str, *, timeout_s: float) -> UserTokens:
        self.refreshed_with.append(refresh_token)
        self.refresh_timeouts.append(timeout_s)
        self.refresh_started.set()
        if self.refresh_gate is not None:
            await self.refresh_gate.wait()
        if self.refused:
            raise UserAuthorizationRevoked("bad_refresh_token")
        return UserTokens(
            access_token="ghu_new",
            access_expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            refresh_token="ghr_new",
            refresh_expires_at=datetime.now(timezone.utc) + timedelta(days=180),
        )


class _Store:
    """One account row, with the lease the real repository keeps."""

    def __init__(self, app: _App, *, expires_in: timedelta | None, lease_held: bool = False):
        self.cipher = app.cipher
        self.row: dict[str, Any] | None = {
            "access_token_ciphertext": app.cipher.encrypt("ghu_old"),
            "access_expires_at": (
                datetime.now(timezone.utc) + expires_in if expires_in is not None else None
            ),
            "refresh_token_ciphertext": app.cipher.encrypt("ghr_old"),
            "refresh_generation": 4,
        }
        self.lease_held = lease_held
        self.written: list[dict[str, Any]] = []
        self.cleared: list[str] = []
        #: Stands in for another caller's renewal landing while this one waits.
        self.on_reread: Any = None
        self.store_result = True
        self.reads = 0
        self.claimed_lease_seconds: int | None = None
        self.after_claim: Any = None
        #: The generation moved under this refresh; the write must not land.
        self.reject_write = False
        #: What the row is once that other caller has moved it.
        self.on_rejected_write: Any = None

    @asynccontextmanager
    async def serialize_github_account(self, *, user_id: str):
        assert user_id == _USER
        yield self

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        assert user_id == _USER
        self.reads += 1
        if self.reads > 1 and self.on_reread is not None:
            self.on_reread(self)
        return dict(self.row) if self.row else None

    async def claim_github_token_refresh(
        self, *, user_id: str, lease_seconds: int, margin_seconds: int
    ) -> dict[str, Any] | None:
        assert margin_seconds == 300
        self.claimed_lease_seconds = lease_seconds
        if self.lease_held:
            return None
        self.lease_held = True
        self.row["refresh_generation"] += 1
        if self.after_claim is not None:
            self.after_claim()
        return {
            "refresh_generation": self.row["refresh_generation"],
            "refresh_token_ciphertext": self.row["refresh_token_ciphertext"],
            "access_token_ciphertext": self.row["access_token_ciphertext"],
        }

    async def store_github_tokens(self, **written: Any) -> bool:
        self.written.append(written)
        if not self.store_result:
            return False
        if self.reject_write:
            if self.on_rejected_write is not None:
                self.on_rejected_write(self)
            return False
        return True

    async def clear_github_tokens(self, *, user_id: str, access_token_ciphertext: str) -> bool:
        self.cleared.append(access_token_ciphertext)
        return True


async def test_a_token_far_from_expiry_is_used_as_it_is():
    app = _App()
    store = _Store(app, expires_in=timedelta(hours=2))

    token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_old"
    assert app.refreshed_with == [] and not store.lease_held


async def test_a_token_that_never_expires_is_used_as_it_is():
    """An App that does not expire user tokens gives no expiry and no refresh token."""
    app = _App()
    store = _Store(app, expires_in=None)

    token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_old"
    assert app.refreshed_with == []


async def test_an_expiring_token_is_renewed_under_the_lease_and_written_back():
    app = _App()
    store = _Store(app, expires_in=timedelta(minutes=2))

    token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_new"
    assert app.refreshed_with == ["ghr_old"]
    assert store.claimed_lease_seconds == 60
    [written] = store.written
    # Written only while the generation it claimed is still the row's.
    assert written["refresh_generation"] == 5
    assert app.cipher.decrypt(written["access_token_ciphertext"]) == "ghu_new"
    assert app.cipher.decrypt(written["refresh_token_ciphertext"]) == "ghr_new"
    assert token.ciphertext == written["access_token_ciphertext"]


async def test_refresh_lease_outlasts_a_delayed_provider_timeout_override():
    app = _App(request_timeout_s=300)
    app.refresh_gate = asyncio.Event()
    store = _Store(app, expires_in=timedelta(minutes=2))

    pending = asyncio.create_task(user_access_token(_USER, store=store, app_credentials=app))
    await asyncio.wait_for(app.refresh_started.wait(), timeout=1)

    assert store.claimed_lease_seconds == 330
    assert not pending.done(), "the provider call should still hold the serialized account lock"

    app.refresh_gate.set()
    token = await pending
    assert token is not None and token.value == "ghu_new"


async def test_github_setting_flip_keeps_one_timeout_for_lease_and_http(monkeypatch):
    """A setting increase during claim applies only to the next refresh."""
    values = {"github_request_timeout_s": 20.0}
    monkeypatch.setattr(
        github_app, "number_setting", lambda name, default=None: values.get(name, default)
    )
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "test-source-control-key")
    request_timeouts: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        request_timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "expires_in": 28800,
                "refresh_token": "ghr_new",
                "refresh_token_expires_in": 15897600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        app = GitHubAppCredentials(
            AppConfig(
                "app",
                "unused-key",
                client_id="Iv1.client",
                client_secret="secret",
            ),
            client=client,
        )
        store = _Store(app, expires_in=timedelta(minutes=2))
        store.after_claim = lambda: values.update(github_request_timeout_s=300.0)
        token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_new"
    assert store.claimed_lease_seconds == 50
    assert request_timeouts == [20.0]


async def test_a_refresh_that_outlives_its_lease_falls_back_to_the_still_live_token():
    app = _App()
    store = _Store(app, expires_in=timedelta(minutes=2))
    store.store_result = False

    token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_old"
    [rejected] = store.written
    assert app.cipher.decrypt(rejected["access_token_ciphertext"]) == "ghu_new"


@pytest.mark.parametrize("outcome", ["reconnect", "no-ciphertext", "no-row"])
async def test_a_refresh_the_fence_rejects_is_not_returned(outcome: str):
    """Reconnect or disconnect moved the generation. The minted token stays unused."""
    app = _App()
    store = _Store(app, expires_in=timedelta(minutes=2))
    store.reject_write = True
    current = app.cipher.encrypt("ghu_reconnected")

    def moved(store: _Store) -> None:
        if outcome == "no-row":
            store.row = None
            return
        assert store.row is not None
        if outcome == "no-ciphertext":
            store.row["access_token_ciphertext"] = None
            return
        store.row["access_token_ciphertext"] = current
        store.row["access_expires_at"] = datetime.now(timezone.utc) + timedelta(hours=8)

    store.on_rejected_write = moved

    token = await user_access_token(_USER, store=store, app_credentials=app)

    rejected = app.cipher.decrypt(store.written[0]["access_token_ciphertext"])
    assert rejected == "ghu_new"
    assert app.refreshed_with == ["ghr_old"]
    if outcome == "reconnect":
        assert token is not None
        assert token.value == "ghu_reconnected"
        assert token.ciphertext == current
    else:
        assert token is None


async def test_a_caller_without_the_lease_uses_the_old_token_while_it_still_works():
    """Renewal starts five minutes early, so the loser has a token that works."""
    app = _App()
    store = _Store(app, expires_in=timedelta(minutes=2), lease_held=True)

    token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_old"
    assert app.refreshed_with == [], "only the lease holder spends the refresh token"


async def test_a_caller_without_the_lease_waits_for_a_lapsed_token_to_be_renewed(monkeypatch):
    monkeypatch.setattr(github_user_token, "_WAIT_INTERVAL_S", 0)
    app = _App()
    store = _Store(app, expires_in=-timedelta(minutes=1), lease_held=True)
    renewed = app.cipher.encrypt("ghu_from_the_other_caller")

    def other_caller_lands(store: _Store) -> None:
        if store.reads == 3:
            store.row["access_token_ciphertext"] = renewed
            store.row["access_expires_at"] = datetime.now(timezone.utc) + timedelta(hours=8)

    store.on_reread = other_caller_lands

    token = await user_access_token(_USER, store=store, app_credentials=app)

    assert token is not None and token.value == "ghu_from_the_other_caller"
    assert app.refreshed_with == []


async def test_a_renewal_that_never_lands_is_a_passing_failure(monkeypatch):
    """Not a reconnect: the lease will lapse and the next caller renews it."""
    monkeypatch.setattr(github_user_token, "_WAIT_INTERVAL_S", 0)
    app = _App()
    store = _Store(app, expires_in=-timedelta(minutes=1), lease_held=True)

    with pytest.raises(UserTokenUnavailable):
        await user_access_token(_USER, store=store, app_credentials=app)
    assert store.cleared == []


async def test_a_refused_renewal_forgets_the_token_the_user_had():
    app = _App(refused=True)
    store = _Store(app, expires_in=timedelta(minutes=2))
    stored = store.row["access_token_ciphertext"]

    assert await user_access_token(_USER, store=store, app_credentials=app) is None
    # Exactly the token that failed: a newer one written meanwhile survives.
    assert store.cleared == [stored]
    assert store.written == []


async def test_no_stored_token_means_connect_again():
    app = _App()
    store = _Store(app, expires_in=timedelta(hours=2))
    store.row["access_token_ciphertext"] = None

    assert await user_access_token(_USER, store=store, app_credentials=app) is None
    store.row = None
    assert await user_access_token(_USER, store=store, app_credentials=app) is None
