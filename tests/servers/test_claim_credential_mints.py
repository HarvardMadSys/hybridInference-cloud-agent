"""The claim asks the gateway and GitHub for its credentials at once.

A turn's inference grant, its clone token and its sandbox GitHub token used to
be minted one after another, though none needs another's answer. They overlap
now, and these cases pin that nothing else moved: whatever each one's outcome,
the claim answers, releases, ends and revokes exactly as when each waited for
the one before, because the first to fail in that old order decides. And a
cancelled request leaves no mint running behind it and settles nothing that a
cancellation did not settle before.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tests.servers.test_grant_at_claim import _ATTEMPT, _JOB, _app, _ClaimStore, _Grants

from cloud_agent.api import worker as agent_worker
from cloud_agent.api._claim_credentials import mint_claim_credentials
from cloud_agent.api.deps import get_agent_app_credentials
from cloud_agent.gateway import GatewayError
from cloud_agent.github_app import AppNotInstalled

pytestmark = pytest.mark.asyncio

#: How each grant failure answers when it decides the claim.
_GRANT_REFUSALS = {
    "refused": (503, "Could not mint an inference grant for this job"),
    "ended": (403, "The gateway account can no longer run this job."),
    "unlinked": (503, "The inference grant for this job could not be recorded"),
}
_GITHUB_ERROR = (503, "Could not mint a repository credential for this job")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "claim-test-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "claim-test-sandbox-manager-secret")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")


class _SubjectGone(_Grants):
    """A gateway that refuses the owner for good."""

    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        raise GatewayError("subject unavailable", status_code=403, error_type="subject_unavailable")


class _GitHub:
    """The App's token minter, answering the clone and sandbox scopes as told.

    ``ok`` mints, ``missing`` is an App that does not cover the repository, and
    ``error`` is GitHub having a bad minute.
    """

    def __init__(self, clone: str = "ok", sandbox: str = "ok") -> None:
        self.answers = {"clone": clone, "sandbox": sandbox}
        self.asked: list[str] = []

    async def token_for(self, repo: str, **kwargs: Any) -> str:
        scope = "clone" if kwargs["permissions"] == {"contents": "read"} else "sandbox"
        self.asked.append(scope)
        answer = self.answers[scope]
        if answer == "missing":
            raise AppNotInstalled(f"no App installation covers {repo}", status=404)
        if answer == "error":
            raise TimeoutError("api.github.com timed out")
        return f"ghs_{scope}"


def _grant_side(outcome: str) -> tuple[_ClaimStore, _Grants]:
    """The store and gateway that give the grant ``outcome``."""
    if outcome == "unlinked":
        return _ClaimStore(fence_still_current=False), _Grants()
    if outcome == "refused":
        return _ClaimStore(), _Grants(fail=True)
    if outcome == "ended":
        return _ClaimStore(), _SubjectGone()
    return _ClaimStore(), _Grants()


async def _claim(store: _ClaimStore, grants: _Grants, github: Any) -> Response:
    app = _app(store, grants)
    app.dependency_overrides[get_agent_app_credentials] = lambda: github
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})


def _answer(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["detail"]["error"]["message"]


@pytest.mark.parametrize(
    ("grant", "clone", "sandbox"),
    list(
        itertools.product(
            ["ok", "refused", "ended", "unlinked"],
            ["ok", "missing", "error"],
            ["ok", "missing", "error"],
        )
    ),
)
async def test_the_first_failure_in_the_old_order_decides_the_claim(grant, clone, sandbox):
    """Grant, then clone token, then sandbox token: the first to fail is the answer.

    What the mints after it got is dropped, as if they had never been asked,
    and the claim is released or ended once, by whichever decided.
    """
    store, grants = _grant_side(grant)
    github = _GitHub(clone, sandbox)

    response = await _claim(store, grants, github)

    assert sorted(github.asked) == ["clone", "sandbox"], (
        "every mint is asked, whatever the rest say"
    )
    if grant != "ok":
        status, message = _GRANT_REFUSALS[grant]
        assert _answer(response)[0] == status
        assert _answer(response)[1].startswith(message)
        assert store.released == ([] if grant == "ended" else [(_JOB, _ATTEMPT)])
        assert store.failed == ([_JOB] if grant == "ended" else [])
        assert grants.revoked == (["agr_7"] if grant == "unlinked" else [])
        assert store.linked == {}
        return
    # The grant is the attempt's now, so the revocation pass collects it once
    # the attempt is released or ends; nothing here revokes it.
    assert store.linked == {_ATTEMPT: "agr_7"}
    assert grants.revoked == []
    assert store.failed == []
    decisive = next((outcome for outcome in (clone, sandbox) if outcome != "ok"), None)
    if decisive == "error":
        assert _answer(response)[0] == _GITHUB_ERROR[0]
        assert _answer(response)[1].startswith(_GITHUB_ERROR[1])
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.retry_delays == [30]
        return
    # An App that does not cover the repository is a settled answer: the
    # tokens minted before it go to the runner, and none after it.
    assert response.status_code == 200, response.text
    assert store.released == []
    assert response.json()["clone_token"] == ("ghs_clone" if clone == "ok" else None)
    assert response.json()["github_token"] == (
        "ghs_sandbox" if clone == "ok" and sandbox == "ok" else None
    )


async def test_the_three_mints_overlap():
    """The claim waits about as long as its slowest mint, not their sum."""
    delay_s = 0.5
    windows: dict[str, tuple[float, float]] = {}

    class SlowGrants(_Grants):
        async def mint(self, **kwargs: Any) -> dict[str, Any]:
            started = time.monotonic()
            await asyncio.sleep(delay_s)
            windows["grant"] = (started, time.monotonic())
            return await super().mint(**kwargs)

    class SlowGitHub(_GitHub):
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            started = time.monotonic()
            await asyncio.sleep(delay_s)
            token = await super().token_for(repo, **kwargs)
            windows[token] = (started, time.monotonic())
            return token

    app = _app(_ClaimStore(), SlowGrants())
    app.dependency_overrides[get_agent_app_credentials] = SlowGitHub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Timed around the request alone: building the app is no part of it.
        started = time.monotonic()
        response = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
        elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert sorted(windows) == ["ghs_clone", "ghs_sandbox", "grant"]
    # Every mint began before any had finished: none waited for another.
    assert max(start for start, _ in windows.values()) < min(end for _, end in windows.values())
    assert elapsed < 2 * delay_s, f"took {elapsed:.2f}s; one at a time takes {3 * delay_s}s"


async def test_a_github_failure_does_not_cut_a_slower_grant_short():
    """The gateway may already have issued the grant; only its link can revoke it.

    GitHub refusing first must not cancel the grant's mint or its link, or the
    grant would live, owned by nothing, until its TTL. It is linked, and the
    released attempt hands it to the revocation pass, as when GitHub was only
    asked after the grant.
    """

    class SlowGrants(_Grants):
        async def mint(self, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            return await super().mint(**kwargs)

    store, grants = _ClaimStore(), SlowGrants()

    response = await _claim(store, grants, _GitHub(clone="error"))

    assert _answer(response)[0] == _GITHUB_ERROR[0]
    assert _answer(response)[1].startswith(_GITHUB_ERROR[1])
    assert store.linked == {_ATTEMPT: "agr_7"}
    assert grants.revoked == []
    assert store.released == [(_JOB, _ATTEMPT)]


class _Held:
    """Mints that answer nothing until cancelled, and record when each ends.

    Each takes a moment of its own to wind down once cancelled, as a request
    cut short does. A claim that gave up before every one had ended would leave
    one running for a request nobody holds any more.
    """

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.started: list[str] = []
        self.ended: list[str] = []
        self.all_started = asyncio.Event()

    async def hold(self, name: str) -> Any:
        self.started.append(name)
        if sorted(self.started) == sorted(self.names):
            self.all_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01 * (1 + self.names.index(name)))
            raise
        finally:
            self.ended.append(name)


@pytest.mark.parametrize("grant", ["linked", "minting"])
async def test_a_cancelled_claim_waits_for_every_mint_and_settles_nothing_new(grant):
    """A cancellation now reaches the mints still running, and only those.

    A grant linked before it is the attempt's, and the revocation pass collects
    it when the lease lapses, as it did when the cancellation landed in a
    GitHub mint. One still being minted is cut short as it was when the
    cancellation landed in the grant's own mint. Neither is released, ended or
    revoked here, then or now: the lease decides.
    """
    held = _Held(["clone", "sandbox"] + (["grant"] if grant == "minting" else []))

    class HeldGrants(_Grants):
        async def mint(self, **kwargs: Any) -> dict[str, Any]:
            if grant == "minting":
                await held.hold("grant")
            return await super().mint(**kwargs)

    class HeldGitHub:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            scope = "clone" if kwargs["permissions"] == {"contents": "read"} else "sandbox"
            return await held.hold(scope)

    store, grants = _ClaimStore(), HeldGrants()
    claim = await store.claim_job(
        worker_id="w1", lease_ttl_seconds=60, host="runner-test", host_credential_hash="hash"
    )
    assert claim is not None
    minting = asyncio.create_task(
        mint_claim_credentials(
            store,
            claim,
            grant=lambda: agent_worker._mint_sandbox_grant(store, grants, claim),
            app_credentials=HeldGitHub(),
        )
    )
    await asyncio.wait_for(held.all_started.wait(), 5)

    minting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await minting

    assert sorted(held.ended) == sorted(held.names), "a mint outlived the claim"
    assert store.linked == ({_ATTEMPT: "agr_7"} if grant == "linked" else {})
    assert grants.revoked == []
    assert store.released == []
    assert store.failed == []
