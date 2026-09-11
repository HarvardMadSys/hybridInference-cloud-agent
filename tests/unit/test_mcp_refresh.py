"""Credential refresh: the claim, the rotation contract, and the disconnect.

Two failures here are worth more than the happy path, because both destroy a
working connection rather than producing an error:

- a second refresher calling the token endpoint with a refresh token the first
  one already rotated away;
- keeping an old refresh token after an issuer that rotates sent none, which
  looks like success until the next refresh hours later.

The third is a race with a disconnect: the owner removed the credential, and a
refresh that started before that must not put it back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud_agent.mcp import refresh
from cloud_agent.mcp.net_policy import EgressPolicy
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.mcp.transports import TransportError

_KEY = "refresh-test-encryption-key"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deployment a key, so a credential can be encrypted at all."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _KEY)


def _row(**overrides: Any) -> dict[str, Any]:
    """A connection with a credential that is already due for a refresh."""
    cipher = mcp_cipher(_KEY)
    row: dict[str, Any] = {
        "id": 7,
        "user_id": "u1",
        "label": "Docs",
        "auth_kind": "oauth",
        "auth_config": {"client_id": "our-client"},
        "access_token_ciphertext": cipher.encrypt("old-access"),
        "refresh_token_ciphertext": cipher.encrypt("old-refresh"),
        "client_secret_ciphertext": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=30),
        "scopes": ["read"],
        "issuer": "https://auth.example.com",
        "resource": "https://mcp.example.com/mcp",
        "refresh_rotates": False,
        "token_endpoint": "https://auth.example.com/token",
        "revocation_endpoint": None,
        "registration_endpoint": None,
        "client_auth_method": "none",
        "registration": "preregistered",
        "credential_version": 4,
        "authorization_epoch": 2,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


class FakeStore:
    """A store whose claim is a real compare-and-set."""

    def __init__(self, row: dict[str, Any]) -> None:
        """Hold one row and count the claims that were attempted."""
        self.row = row
        self.claims = 0
        self.writes: list[dict[str, Any]] = []

    async def claim_mcp_refresh(
        self, *, connection_id: int, expected_credential_version: int
    ) -> dict[str, Any] | None:
        self.claims += 1
        if self.row["credential_version"] != expected_credential_version:
            return None
        self.row["credential_version"] += 1
        return dict(self.row)

    async def set_mcp_credential(self, **kwargs: Any) -> bool:
        self.writes.append(kwargs)
        if self.row["authorization_epoch"] != kwargs["expected_authorization_epoch"]:
            return False
        self.row["access_token_ciphertext"] = kwargs.get("access_token_ciphertext")
        if kwargs.get("refresh_token_ciphertext"):
            self.row["refresh_token_ciphertext"] = kwargs["refresh_token_ciphertext"]
        return True

    async def get_mcp_connection(self, *, connection_id: int) -> dict[str, Any] | None:
        return dict(self.row)


def _patch_token_response(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[Any]:
    """Answer the token endpoint with ``payload``, recording each request."""
    seen: list[Any] = []

    async def fake_post(request: Any, *, policy: Any, fetcher: Any) -> dict[str, Any]:
        seen.append(request)
        return payload

    monkeypatch.setattr(refresh, "_post", fake_post)
    return seen


class TestWhenToRefresh:
    """The margin, and what counts as refreshable."""

    def test_a_token_inside_the_margin_is_refreshed(self) -> None:
        """A tool call can take minutes, so renewal is not left to expiry."""
        assert refresh.should_refresh(_row())

    def test_a_comfortably_valid_token_is_left_alone(self) -> None:
        """Refreshing on every call would hammer the authorization server."""
        row = _row(expires_at=datetime.now(timezone.utc) + timedelta(hours=2))
        assert not refresh.should_refresh(row)

    def test_no_expiry_means_no_refresh(self) -> None:
        """An issuer that gave no expiry is taken at its word."""
        assert not refresh.should_refresh(_row(expires_at=None))

    def test_a_static_credential_is_never_refreshed(self) -> None:
        """There is no refresh token, and inventing one would be a bug."""
        assert not refresh.should_refresh(_row(auth_kind="static"))


class TestRefresh:
    """The refresh itself, and the races around it."""

    @pytest.mark.asyncio
    async def test_a_due_credential_is_renewed_and_stored(self, monkeypatch) -> None:
        """The ordinary case, through the cipher both ways."""
        store = FakeStore(_row())
        _patch_token_response(monkeypatch, {"access_token": "new-access", "expires_in": 3600})
        outcome = await refresh.refresh_credential(
            store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
        )
        assert outcome.refreshed
        assert store.writes and store.writes[0]["access_token_ciphertext"]
        cipher = mcp_cipher(_KEY)
        assert cipher.decrypt(store.writes[0]["access_token_ciphertext"]) == "new-access"

    @pytest.mark.asyncio
    async def test_a_lost_claim_does_not_call_the_token_endpoint(self, monkeypatch) -> None:
        """A second call with a rotated refresh token would destroy the connection."""
        store = FakeStore(_row(credential_version=9))
        seen = _patch_token_response(monkeypatch, {"access_token": "new"})
        outcome = await refresh.refresh_credential(
            store, row=_row(credential_version=8), policy=EgressPolicy()  # type: ignore[arg-type]
        )
        assert outcome.contended
        assert not outcome.refreshed
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_non_rotating_issuer_keeps_the_refresh_token(self, monkeypatch) -> None:
        """Omitting one there means "keep using the old one"."""
        store = FakeStore(_row(refresh_rotates=False))
        _patch_token_response(monkeypatch, {"access_token": "new"})
        await refresh.refresh_credential(
            store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
        )
        assert store.writes[0]["refresh_rotates"] is False
        assert store.writes[0]["refresh_token_ciphertext"]

    @pytest.mark.asyncio
    async def test_a_rotating_issuer_that_sent_nothing_is_refused(self, monkeypatch) -> None:
        """Keeping the old token would look fine until the next refresh."""
        store = FakeStore(_row(refresh_rotates=True))
        _patch_token_response(monkeypatch, {"access_token": "new"})
        with pytest.raises(refresh.RefreshRefused):
            await refresh.refresh_credential(
                store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
            )
        assert store.writes == []

    @pytest.mark.asyncio
    async def test_a_disconnect_during_a_refresh_is_not_undone(self, monkeypatch) -> None:
        """The owner removed the credential; their decision wins."""
        store = FakeStore(_row())
        _patch_token_response(monkeypatch, {"access_token": "new", "expires_in": 60})

        async def failing_write(**kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(store, "set_mcp_credential", failing_write)
        with pytest.raises(refresh.RefreshRefused):
            await refresh.refresh_credential(
                store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_a_token_endpoint_failure_is_reported_not_swallowed(self, monkeypatch) -> None:
        """A connection whose refresh fails must say so."""

        async def failing_post(request: Any, *, policy: Any, fetcher: Any) -> dict[str, Any]:
            raise TransportError("the token endpoint answered 400")

        monkeypatch.setattr(refresh, "_post", failing_post)
        store = FakeStore(_row())
        with pytest.raises(refresh.RefreshRefused):
            await refresh.refresh_credential(
                store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_a_connection_with_no_recorded_token_endpoint_is_refused(self) -> None:
        """Guessing one from the issuer's origin is right often enough to hurt."""
        store = FakeStore(_row(token_endpoint=None))
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(
                store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
            )
        assert raised.value.kind == "protocol_incompatible"

    @pytest.mark.asyncio
    async def test_an_expired_connection_with_no_refresh_token_says_reconnect(self) -> None:
        """There is nothing to try, and "expired" is what the user can act on."""
        store = FakeStore(
            _row(refresh_token_ciphertext=None, expires_at=datetime.now(timezone.utc))
        )
        with pytest.raises(refresh.RefreshRefused):
            await refresh.refresh_credential(
                store, row=store.row, policy=EgressPolicy()  # type: ignore[arg-type]
            )


class TestEnsureFresh:
    """The one entry point callers should use."""

    @pytest.mark.asyncio
    async def test_a_fresh_credential_costs_nothing(self, monkeypatch) -> None:
        """No claim, no request: the common case is a dictionary read."""
        store = FakeStore(_row(expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        row = await refresh.ensure_fresh(store, row=store.row)  # type: ignore[arg-type]
        assert store.claims == 0
        assert row["id"] == 7

    @pytest.mark.asyncio
    async def test_a_contended_refresh_returns_what_is_there_now(self, monkeypatch) -> None:
        """Re-reading beats waiting: the other instance's write is newer."""
        store = FakeStore(_row(credential_version=9))
        row = await refresh.ensure_fresh(
            store, row=_row(credential_version=8)  # type: ignore[arg-type]
        )
        assert row["credential_version"] == 9
