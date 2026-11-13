"""The provisioning pass: queue, invite, complete, revoke — with fakes.

The pass is the only place the lifecycle is sequenced, so its decisions are
pinned here without a database or a network: which rows are minted for,
what the mail carries, and what a refusal leaves behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cloud_agent.application import account_provisioning as provisioning


class FakeRepo:
    """Provisioning rows in memory, recording every write."""

    def __init__(self, *, pending=(), invited=(), ended=()):
        self._pending = list(pending)
        self._invited = list(invited)
        self._ended = list(ended)
        self.writes: list[tuple] = []
        self.activated = datetime(2026, 11, 13, tzinfo=UTC)

    async def ensure_activation(self):
        return self.activated

    async def enqueue_new_approvals(self, *, activated_at, limit=50):
        self.writes.append(("enqueue", activated_at))
        return 0

    async def access_ended(self, *, limit=50):
        return self._ended

    async def pending(self, *, limit=20):
        return self._pending

    async def invited(self, *, limit=50):
        return self._invited

    async def record_attempt(self, account_id, *, error):
        self.writes.append(("attempt", account_id, error))

    async def mark_invited(self, account_id):
        self.writes.append(("invited", account_id))

    async def mark_provisioned(self, account_id):
        self.writes.append(("provisioned", account_id))

    async def mark_revoked(self, account_id, *, audited):
        self.writes.append(("revoked", account_id, audited))


class FakeClient:
    """The inference service's answers, scripted per account."""

    def __init__(self, *, mint_results=None, provisioned_ids=(), fail_mint=()):
        self._mint = mint_results or {}
        self._provisioned = set(provisioned_ids)
        self._fail_mint = set(fail_mint)
        self.revoked: list[str] = []

    async def mint(self, *, account_id, email):
        if account_id in self._fail_mint:
            raise provisioning.ProvisioningRefused("mint answered 503")
        return self._mint.get(account_id, ("minted", f"token-{account_id}"))

    async def provisioned(self, *, account_id):
        return account_id in self._provisioned

    async def revoke(self, *, account_id):
        self.revoked.append(account_id)


class FakeMailer:
    def __init__(self, *, delivered=True):
        self.sent: list = []
        self._delivered = delivered

    async def send(self, mail):
        self.sent.append(mail)
        return self._delivered


def _storage(repo) -> SimpleNamespace:
    return SimpleNamespace(account_provisioning=repo)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(provisioning, "boolean_setting", lambda key: True)


async def _run(repo, client, mailer):
    await provisioning.run_provisioning_pass(
        _storage(repo), mailer=mailer, client=client, web_url="https://inference.example"
    )


@pytest.mark.asyncio
async def test_a_pending_account_is_minted_and_mailed_a_fragment_link():
    repo = FakeRepo(pending=[{"account_id": "a1", "email": "a@example.org", "attempts": 0}])
    mailer = FakeMailer()

    await _run(repo, FakeClient(), mailer)

    assert repo.writes[0][0] == "enqueue"
    assert ("invited", "a1") in repo.writes
    assert len(mailer.sent) == 1
    mail = mailer.sent[0]
    assert mail.kind == "inference_invite"
    assert mail.to == "a@example.org"
    # The token rides the fragment: browsers never send it to a server.
    assert mail.link == "https://inference.example/agent-invite#token=token-a1"


@pytest.mark.asyncio
async def test_undelivered_mail_leaves_the_row_pending():
    repo = FakeRepo(pending=[{"account_id": "a1", "email": "a@example.org", "attempts": 0}])

    await _run(repo, FakeClient(), FakeMailer(delivered=False))

    assert ("invited", "a1") not in repo.writes
    assert repo.writes[-1][0] == "attempt"


@pytest.mark.asyncio
async def test_a_refused_mint_leaves_the_row_pending():
    repo = FakeRepo(pending=[{"account_id": "a1", "email": "a@example.org", "attempts": 0}])

    await _run(repo, FakeClient(fail_mint={"a1"}), FakeMailer())

    assert ("invited", "a1") not in repo.writes
    assert repo.writes[-1][0] == "attempt"


@pytest.mark.asyncio
async def test_an_already_redeemed_account_is_marked_provisioned_without_mail():
    repo = FakeRepo(pending=[{"account_id": "a1", "email": "a@example.org", "attempts": 0}])
    mailer = FakeMailer()

    await _run(repo, FakeClient(mint_results={"a1": ("already", None)}), mailer)

    assert ("provisioned", "a1") in repo.writes
    assert mailer.sent == []


@pytest.mark.asyncio
async def test_an_invited_account_that_redeemed_is_marked_provisioned():
    repo = FakeRepo(invited=[{"account_id": "a2", "email": "b@example.org"}])

    await _run(repo, FakeClient(provisioned_ids={"a2"}), FakeMailer())

    assert ("provisioned", "a2") in repo.writes


@pytest.mark.asyncio
async def test_ended_access_revokes_provisioned_and_drops_pending():
    repo = FakeRepo(
        ended=[
            {"account_id": "gone-pending", "state": "pending"},
            {"account_id": "gone-live", "state": "provisioned"},
        ]
    )
    client = FakeClient()

    await _run(repo, client, FakeMailer())

    assert client.revoked == ["gone-live"]
    assert ("revoked", "gone-pending", False) in repo.writes
    assert ("revoked", "gone-live", True) in repo.writes


class TestClientContract:
    """The worker's HTTP calls match the inference service's routes."""

    def _client(self, handler) -> provisioning.InferenceProvisioningClient:
        import httpx

        return provisioning.InferenceProvisioningClient(
            "https://gw.example/", "dispatch-secret", transport=httpx.MockTransport(handler)
        )

    @pytest.mark.asyncio
    async def test_mint_posts_to_the_internal_route_with_the_dispatch_token(self):
        seen = {}

        def handler(request):
            import httpx

            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(201, json={"token": "tok-1", "expires_in": 60})

        client = self._client(handler)
        outcome, token = await client.mint(account_id="a1", email="a@example.org")

        assert (outcome, token) == ("minted", "tok-1")
        assert seen["url"] == "https://gw.example/internal/agent-invitations"
        assert seen["auth"] == "Bearer dispatch-secret"

    @pytest.mark.asyncio
    async def test_mint_409_means_already_redeemed(self):
        import httpx

        client = self._client(lambda request: httpx.Response(409, json={}))
        assert await client.mint(account_id="a1", email="a@example.org") == ("already", None)

    @pytest.mark.asyncio
    async def test_status_and_revoke(self):
        import httpx

        def handler(request):
            if request.method == "GET":
                assert request.url.path == "/internal/agent-invitations/a1"
                return httpx.Response(200, json={"provisioned": True, "revoked": False})
            assert request.url.path == "/internal/agent-invitations/revoke"
            return httpx.Response(200, json={"revoked": True})

        client = self._client(handler)
        assert await client.provisioned(account_id="a1") is True
        await client.revoke(account_id="a1")


@pytest.mark.asyncio
async def test_disabled_provisioning_does_nothing(monkeypatch):
    monkeypatch.setattr(provisioning, "boolean_setting", lambda key: False)
    repo = FakeRepo(pending=[{"account_id": "a1", "email": "a@example.org", "attempts": 0}])

    await provisioning.run_provisioning_pass(
        _storage(repo), mailer=FakeMailer(), client=FakeClient(), web_url="https://x.example"
    )

    assert repo.writes == []
