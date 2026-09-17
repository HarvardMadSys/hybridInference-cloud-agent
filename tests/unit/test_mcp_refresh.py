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
        "oauth_generation": 3,
        "config_revision": 5,
        "refresh_generation": 0,
        "refresh_lease_expires_at": None,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


class FakeStore:
    """A store whose claim is a real lease, and whose write is fenced on it.

    The lease is modelled here rather than asserted against, so the cases below
    exercise ``refresh``'s handling of it: contended, released, superseded. The
    SQL that implements the same contract is pinned against a real database in
    ``tests/integration/storage/test_mcp_connection_store.py``.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        """Hold one row, count the claims, and remember which leases are held."""
        self.row = row
        self.claims = 0
        self.writes: list[dict[str, Any]] = []
        self.leased: set[int] = set()
        self.released: list[int] = []

    async def claim_mcp_refresh(
        self,
        *,
        connection_id: int,
        expected_credential_version: int,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        self.claims += 1
        if self.row["credential_version"] != expected_credential_version:
            return None
        generation = int(self.row.get("refresh_generation") or 0)
        if generation in self.leased:
            # Somebody is already on the network with this token.
            return None
        if lease_seconds <= 0:
            raise ValueError("an MCP refresh lease must be positive")
        self.row["refresh_generation"] = generation + 1
        self.leased.add(generation + 1)
        return dict(self.row)

    async def release_mcp_refresh_claim(
        self, *, connection_id: int, refresh_generation: int
    ) -> bool:
        if refresh_generation not in self.leased:
            return False
        self.leased.discard(refresh_generation)
        self.released.append(refresh_generation)
        return True

    async def set_mcp_credential(self, **kwargs: Any) -> bool:
        self.writes.append(kwargs)
        if self.row["authorization_epoch"] != kwargs["expected_authorization_epoch"]:
            return False
        generation = kwargs.get("expected_refresh_generation")
        if generation is not None:
            if int(self.row.get("refresh_generation") or 0) != generation:
                return False
            if generation not in self.leased:
                return False
            self.leased.discard(generation)
        for key, column in (
            ("expected_oauth_generation", "oauth_generation"),
            ("expected_config_revision", "config_revision"),
        ):
            expected = kwargs.get(key)
            if expected is not None and int(self.row.get(column) or 0) != expected:
                return False
        self.row["access_token_ciphertext"] = kwargs.get("access_token_ciphertext")
        if kwargs.get("refresh_token_ciphertext"):
            self.row["refresh_token_ciphertext"] = kwargs["refresh_token_ciphertext"]
        self.row["credential_version"] = int(self.row["credential_version"]) + 1
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


def _client_that_fails(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Make the *real* ``_post`` meet a transport failure.

    ``_post`` is deliberately not stubbed here: what these cases are about is
    the half the rest of the suite replaces -- what the network layer raises and
    what the refresh does with the lease as a result.
    """
    import httpx

    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> Any:
        def handler(request: Any) -> Any:
            raise error

        # The production call pins the transport it dials with; this fake
        # supplies the transport it wants the failure to come from instead.
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _client_that_answers(monkeypatch: pytest.MonkeyPatch, *, status_code: int, text: str) -> None:
    """Make the *real* ``_post`` receive an answer of this shape."""
    import httpx

    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> Any:
        def handler(request: Any) -> Any:
            return httpx.Response(status_code, text=text)

        # Same as above: the fake's transport replaces the pinned one.
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


#: A token endpoint at a literal loopback address, so the real ``_post`` resolves
#: nothing and the transport failure under test is the first thing it meets.
_LOOPBACK_TOKEN_ENDPOINT = "https://127.0.0.1/token"
_ALLOW_LOOPBACK = EgressPolicy(allow_private=True)


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
            store,
            row=store.row,
            policy=EgressPolicy(),  # type: ignore[arg-type]
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
            store,
            row=_row(credential_version=8),
            policy=EgressPolicy(),  # type: ignore[arg-type]
        )
        assert outcome.contended
        assert not outcome.refreshed
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_second_refresher_is_refused_while_the_lease_is_held(self, monkeypatch) -> None:
        """The race the credential version could not see.

        A second worker arrives while the first is on the network with the
        refresh token. It reads the row *after* the first claimed it, so the
        version it sees is the version it claims -- a claim no longer moves that
        column, which is exactly why it could not be the lock. Only the lease
        tells it that somebody is already spending the token both of them read,
        and a rotating issuer invalidates that token the moment it is used
        twice.
        """
        store = FakeStore(_row())
        nested: list[Any] = []
        arrived = False

        async def post_that_races(request: Any, *, policy: Any, fetcher: Any) -> dict[str, Any]:
            nonlocal arrived
            if not arrived:
                arrived = True
                nested.append(
                    await refresh.refresh_credential(
                        store,
                        row=await store.get_mcp_connection(connection_id=7),
                        policy=policy,
                    )
                )
            return {"access_token": "new-access", "expires_in": 3600}

        monkeypatch.setattr(refresh, "_post", post_that_races)
        first = await refresh.refresh_credential(
            store,
            row=store.row,
            policy=EgressPolicy(),  # type: ignore[arg-type]
        )
        assert first.refreshed
        assert len(nested) == 1
        assert nested[0].contended
        assert not nested[0].refreshed

    @pytest.mark.asyncio
    async def test_a_superseded_refresher_cannot_write_its_answer(self, monkeypatch) -> None:
        """The lease expired under it, and a successor claimed the row.

        The answer it holds was computed from a token the successor has already
        replaced, so writing it would leave the row holding the *older* of the
        two credentials while the newer one is discarded.
        """
        store = FakeStore(_row())

        async def post_that_supersedes(
            request: Any, *, policy: Any, fetcher: Any
        ) -> dict[str, Any]:
            store.leased.clear()
            store.row["refresh_generation"] = 99
            return {"access_token": "stale-access", "expires_in": 3600}

        monkeypatch.setattr(refresh, "_post", post_that_supersedes)
        with pytest.raises(refresh.RefreshRefused):
            await refresh.refresh_credential(
                store,
                row=store.row,
                policy=EgressPolicy(),  # type: ignore[arg-type]
            )
        cipher = mcp_cipher(_KEY)
        assert cipher.decrypt(store.row["access_token_ciphertext"]) == "old-access"

    @pytest.mark.asyncio
    async def test_a_confidential_client_reads_its_secret_under_the_oauth_domain(
        self, monkeypatch
    ) -> None:
        """The client secret and the tokens are wrapped under different domains.

        Reading the secret with the token cipher is not a subtly wrong value: it
        fails at decryption, so every confidential client reported a credential
        that "could not be read" and never reached the token endpoint at all.
        The secret here is wrapped exactly where the authorization flow leaves
        it, which is the part a test that hands in its own cipher cannot show.
        """
        import base64

        from cloud_agent.mcp.registry import oauth_cipher

        store = FakeStore(
            _row(
                client_secret_ciphertext=oauth_cipher(_KEY).encrypt("s3cret"),
                client_auth_method="client_secret_basic",
            )
        )
        seen = _patch_token_response(monkeypatch, {"access_token": "new", "expires_in": 3600})
        outcome = await refresh.refresh_credential(
            store,
            row=store.row,
            policy=EgressPolicy(),  # type: ignore[arg-type]
        )
        assert outcome.refreshed
        assert len(seen) == 1
        # Negotiated as basic, so the secret travels in the header and not in
        # the body -- the mechanism the server agreed to, not a second one.
        credentials = base64.b64decode(
            seen[0].headers()["authorization"].removeprefix("Basic ")
        ).decode()
        assert credentials == "our-client:s3cret"
        assert "client_secret" not in seen[0].form()

    @pytest.mark.asyncio
    async def test_a_refresh_fences_its_write_on_what_it_claimed(self, monkeypatch) -> None:
        """The callback fence, seen from the other side.

        A refresh spends the same time on the network as an authorization-code
        exchange, and the owner can start a new authorization inside that
        window. Both writes carry the epoch, because re-authorizing is not a
        change of target, so the generation the flow began under is what tells
        them apart -- without it the slower write wins and the owner's newer
        account is replaced by an answer computed from the older one.
        """
        store = FakeStore(_row(oauth_generation=3, config_revision=5))
        _patch_token_response(monkeypatch, {"access_token": "new-access", "expires_in": 3600})
        await refresh.refresh_credential(
            store,
            row=store.row,
            policy=EgressPolicy(),  # type: ignore[arg-type]
        )
        write = store.writes[0]
        assert write["expected_refresh_generation"] == 1
        assert write["expected_oauth_generation"] == 3
        assert write["expected_config_revision"] == 5

        store = FakeStore(_row(oauth_generation=3, config_revision=5))

        async def post_that_reauthorizes(
            request: Any, *, policy: Any, fetcher: Any
        ) -> dict[str, Any]:
            # A newer flow finishes while this refresh is still on the network.
            store.row["oauth_generation"] = 4
            return {"access_token": "stale-access", "expires_in": 3600}

        monkeypatch.setattr(refresh, "_post", post_that_reauthorizes)
        with pytest.raises(refresh.RefreshRefused):
            await refresh.refresh_credential(
                store,
                row=store.row,
                policy=EgressPolicy(),  # type: ignore[arg-type]
            )
        cipher = mcp_cipher(_KEY)
        assert cipher.decrypt(store.row["access_token_ciphertext"]) == "old-access"

    @pytest.mark.asyncio
    async def test_a_non_rotating_issuer_keeps_the_refresh_token(self, monkeypatch) -> None:
        """Omitting one there means "keep using the old one"."""
        store = FakeStore(_row(refresh_rotates=False))
        _patch_token_response(monkeypatch, {"access_token": "new"})
        await refresh.refresh_credential(
            store,
            row=store.row,
            policy=EgressPolicy(),  # type: ignore[arg-type]
        )
        assert store.writes[0]["refresh_rotates"] is False
        assert store.writes[0]["refresh_token_ciphertext"]

    @pytest.mark.asyncio
    async def test_an_issuer_that_rotates_once_keeps_the_stored_token_when_none_arrives(
        self, monkeypatch
    ) -> None:
        """An omission is "keep the one you have", not "it is dead".

        RFC 6749 §6 lets an issuer rotate on one refresh and omit the field on
        another, so an omission carries no information about the stored token.
        Refusing here reconnected a working connection; an issuer that really
        did invalidate it refuses the next refresh by name, which is an answer
        the owner can act on.
        """
        store = FakeStore(_row(refresh_rotates=True))
        _patch_token_response(monkeypatch, {"access_token": "new"})
        outcome = await refresh.refresh_credential(
            store,
            row=store.row,
            policy=EgressPolicy(),  # type: ignore[arg-type]
        )
        assert outcome.refreshed is True
        assert store.writes[0]["refresh_rotates"] is True
        # The stored token is kept, so the write still carries one.
        assert store.writes[0]["refresh_token_ciphertext"]

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
                store,
                row=store.row,
                policy=EgressPolicy(),  # type: ignore[arg-type]
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
                store,
                row=store.row,
                policy=EgressPolicy(),  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_a_connection_with_no_recorded_token_endpoint_is_refused(self) -> None:
        """Guessing one from the issuer's origin is right often enough to hurt."""
        store = FakeStore(_row(token_endpoint=None))
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(
                store,
                row=store.row,
                policy=EgressPolicy(),  # type: ignore[arg-type]
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
                store,
                row=store.row,
                policy=EgressPolicy(),  # type: ignore[arg-type]
            )


class TestTheLeaseAfterANetworkFailure:
    """What a transport failure does to the claim, through the real ``_post``.

    A raw ``httpx`` exception used to leave this function -- no diagnosis for
    the caller, and a lease nobody released, so the next attempt was told
    "somebody else is refreshing" for the rest of the minute and then failed the
    same way. The distinction that decides the lease is whether the request
    could have reached the authorization server at all.
    """

    @pytest.mark.asyncio
    async def test_a_connection_that_never_opened_releases_the_lease(self, monkeypatch) -> None:
        """Nothing was sent, so nothing rotated: the next call may retry."""
        import httpx

        store = FakeStore(_row(token_endpoint=_LOOPBACK_TOKEN_ENDPOINT))
        _client_that_fails(monkeypatch, httpx.ConnectError("connection refused"))
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert not raised.value.uncertain
        assert raised.value.kind == "unreachable"
        assert store.released == [1]
        assert store.leased == set()

    @pytest.mark.asyncio
    async def test_a_timeout_after_the_request_left_keeps_the_lease(self, monkeypatch) -> None:
        """The token may already have rotated, so nobody retries at once.

        Holding the lease is what makes the retry *wait*: a second attempt with
        the refresh token still in hand is what a rotating issuer's reuse
        detection reads as a stolen grant, and the wait is bounded by the lease
        rather than by anybody noticing.
        """
        import httpx

        store = FakeStore(_row(token_endpoint=_LOOPBACK_TOKEN_ENDPOINT))
        _client_that_fails(monkeypatch, httpx.ReadTimeout("timed out"))
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert raised.value.uncertain
        assert store.released == []
        assert store.leased == {1}
        # And the lease is what the next caller meets, rather than the network.
        outcome = await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert outcome.contended

    @pytest.mark.asyncio
    async def test_an_answer_that_cannot_be_read_keeps_the_lease(self, monkeypatch) -> None:
        """A 2xx we cannot parse may still have issued the tokens."""

        store = FakeStore(_row(token_endpoint=_LOOPBACK_TOKEN_ENDPOINT))
        _client_that_answers(monkeypatch, status_code=200, text="<html>not json</html>")
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert raised.value.uncertain
        assert store.leased == {1}

    @pytest.mark.asyncio
    async def test_an_answered_refusal_releases_the_lease(self, monkeypatch) -> None:
        """The other side of the line: the endpoint answered, and said no.

        A named OAuth error is the authorization server itself declining to
        exchange the grant, which is the one thing a response can prove. That is
        what makes it safe to retry at once.
        """
        store = FakeStore(_row(token_endpoint=_LOOPBACK_TOKEN_ENDPOINT))
        _client_that_answers(monkeypatch, status_code=400, text='{"error": "invalid_grant"}')
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert not raised.value.uncertain
        assert raised.value.kind == "needs_authorization"
        assert "invalid_grant" in str(raised.value)
        assert store.released == [1]
        # And the row is claimable again: a refused attempt is not a lock.
        _patch_token_response(monkeypatch, {"access_token": "new-access", "expires_in": 3600})
        outcome = await refresh.refresh_credential(
            store,
            row=store.row,
            policy=_ALLOW_LOOPBACK,
        )
        assert outcome.refreshed

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (502, '{"error": "bad_gateway"}'),
            (504, '{"error": "gateway_timeout"}'),
            (400, '{"error": "bad_gateway"}'),
            (200, "[]"),
            (200, '{"refresh_token": "rotated"}'),
        ],
        ids=["bad-gateway", "gateway-timeout", "unnamed-4xx", "non-object", "no-access-token"],
    )
    @pytest.mark.asyncio
    async def test_a_response_that_proves_nothing_keeps_the_lease(
        self, status: int, body: str, monkeypatch
    ) -> None:
        """A status code is not a refusal, and an answer is not a credential.

        A 502 or 504 is produced by whatever sits in front of the issuer --
        RFC 9110 describes both as the upstream failing to answer in time, not
        as it declining to act -- and the issuer may have rotated the refresh
        token before that gateway gave up. A 2xx that carries no usable token
        response may likewise be an exchange that completed and an answer that
        was mangled on the way. Releasing the lease on any of these replays the
        token immediately, which is what a rotating issuer's reuse detection
        reads as a stolen grant.
        """
        store = FakeStore(_row(token_endpoint=_LOOPBACK_TOKEN_ENDPOINT))
        _client_that_answers(monkeypatch, status_code=status, text=body)
        with pytest.raises(refresh.RefreshRefused) as raised:
            await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert raised.value.uncertain
        assert store.released == []
        assert store.leased == {1}
        # The lease, not the network, is what the next caller meets.
        second = await refresh.refresh_credential(store, row=store.row, policy=_ALLOW_LOOPBACK)
        assert second.contended

    @pytest.mark.asyncio
    async def test_a_target_the_policy_refuses_releases_the_lease(self, monkeypatch) -> None:
        """Refused before the socket: certain, so the lease goes straight back."""
        store = FakeStore(_row(token_endpoint=_LOOPBACK_TOKEN_ENDPOINT))
        with pytest.raises(refresh.RefreshRefused) as raised:
            # The default policy refuses a loopback target, and does so before
            # anything is dialled.
            await refresh.refresh_credential(store, row=store.row, policy=EgressPolicy())
        assert raised.value.kind == "blocked_target"
        assert not raised.value.uncertain
        assert store.released == [1]


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
            store,
            row=_row(credential_version=8),  # type: ignore[arg-type]
        )
        assert row["credential_version"] == 9

    @pytest.mark.asyncio
    async def test_an_expired_token_waits_for_the_existing_refresher(self, monkeypatch):
        import asyncio

        store = FakeStore(
            _row(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                refresh_lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            )
        )
        store.leased.add(0)
        read = asyncio.Event()
        original = store.get_mcp_connection

        async def get(**kwargs):
            read.set()
            return await original(**kwargs)

        monkeypatch.setattr(store, "get_mcp_connection", get)
        task = asyncio.create_task(refresh.ensure_fresh(store, row=dict(store.row)))
        await asyncio.wait_for(read.wait(), 1)
        assert not task.done()
        store.row.update(
            access_token_ciphertext=mcp_cipher(_KEY).encrypt("fresh-access"),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            credential_version=5,
            refresh_lease_expires_at=None,
        )
        row = await asyncio.wait_for(task, 2)
        assert mcp_cipher(_KEY).decrypt(row["access_token_ciphertext"]) == "fresh-access"
        assert store.claims == 1
        assert store.writes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change", ["no-lease", "expired-lease", "timeout", "disabled", "deleted"]
    )
    async def test_an_expired_contended_token_is_never_returned(self, monkeypatch, change):
        store = FakeStore(
            _row(
                credential_version=5,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        if change == "expired-lease":
            store.row["refresh_lease_expires_at"] = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
        elif change == "timeout":
            monkeypatch.setattr(refresh, "REFRESH_LEASE_SECONDS", 0.01)
            store.row["refresh_lease_expires_at"] = datetime.now(timezone.utc) + timedelta(
                seconds=60
            )
        elif change == "disabled":
            store.row["status"] = "disabled"
        elif change == "deleted":
            store.row["deleted_at"] = datetime.now(timezone.utc)
        with pytest.raises(refresh.RefreshRefused):
            await refresh.ensure_fresh(store, row={**store.row, "credential_version": 4})
        assert store.claims == 1
        assert store.writes == []

    @pytest.mark.asyncio
    async def test_cancelling_a_refresh_wait_does_not_release_another_workers_lease(self):
        import asyncio

        store = FakeStore(
            _row(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                refresh_lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            )
        )
        store.leased.add(0)
        task = asyncio.create_task(refresh.ensure_fresh(store, row=dict(store.row)))
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.leased == {0}
        assert store.released == []
        assert store.claims == 1
