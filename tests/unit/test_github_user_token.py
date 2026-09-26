"""Keeping a user's GitHub token usable: when it is renewed, and by whom.

A GitHub App's refresh token works once. Two callers renewing the same token
would spend it twice, and GitHub would refuse the second — destroying the
connection both were keeping. These pin the lease that prevents that, and what
each caller does when it does not hold it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import cloud_agent.github_user_token as github_user_token
from cloud_agent.github_app import UserAuthorizationRevoked, UserTokens
from cloud_agent.github_user_token import UserTokenUnavailable, user_access_token
from cloud_agent.source_control import SourceControlCipher

_USER = "user-owner"


class _App:
    """The App's refresh endpoint, counted."""

    def __init__(self, *, refused: bool = False) -> None:
        self.cipher = SourceControlCipher("test-source-control-key")
        self.refused = refused
        self.refreshed_with: list[str] = []

    async def refresh_user_token(self, refresh_token: str) -> UserTokens:
        self.refreshed_with.append(refresh_token)
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
        self.reads = 0

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        assert user_id == _USER
        self.reads += 1
        if self.reads > 1 and self.on_reread is not None:
            self.on_reread(self)
        return dict(self.row) if self.row else None

    async def claim_github_token_refresh(
        self, *, user_id: str, lease_seconds: int, margin_seconds: int
    ) -> dict[str, Any] | None:
        assert (lease_seconds, margin_seconds) == (30, 300)
        if self.lease_held:
            return None
        self.lease_held = True
        self.row["refresh_generation"] += 1
        return {
            "refresh_generation": self.row["refresh_generation"],
            "refresh_token_ciphertext": self.row["refresh_token_ciphertext"],
        }

    async def store_github_tokens(self, **written: Any) -> bool:
        self.written.append(written)
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
    [written] = store.written
    # Written only while the generation it claimed is still the row's.
    assert written["refresh_generation"] == 5
    assert app.cipher.decrypt(written["access_token_ciphertext"]) == "ghu_new"
    assert app.cipher.decrypt(written["refresh_token_ciphertext"]) == "ghr_new"
    assert token.ciphertext == written["access_token_ciphertext"]


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
