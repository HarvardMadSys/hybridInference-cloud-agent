"""The claim asks GitHub for its two tokens at once.

A turn's clone token and its sandbox GitHub token used to be minted one after
the other, though neither needs the other's answer. They overlap now, and these
cases pin that nothing else moved: whatever each one's outcome, the claim
answers and releases exactly as when the second waited for the first, because
the first to fail in that old order decides. And a cancelled request leaves no
mint running behind it and settles nothing.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tests.servers.test_worker_claim import _ATTEMPT, _JOB, _app, _ClaimStore
from tests.support.owners import connection_claim

from cloud_agent.api._claim_credentials import mint_claim_credentials
from cloud_agent.api.deps import get_agent_app_credentials
from cloud_agent.github_app import AppNotInstalled

pytestmark = pytest.mark.asyncio

_GITHUB_ERROR = (503, "Could not mint a repository credential for this job")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "claim-test-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "claim-test-sandbox-manager-secret")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    monkeypatch.setenv(
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "claim-test-connection-encryption-key"
    )


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


async def _claim(store: _ClaimStore, github: Any) -> Response:
    app = _app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: github
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )


def _answer(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["detail"]["error"]["message"]


@pytest.mark.parametrize(
    ("clone", "sandbox"), list(itertools.product(["ok", "missing", "error"], repeat=2))
)
async def test_the_first_failure_in_the_old_order_decides_the_claim(clone, sandbox):
    """Clone token, then sandbox token: the first to fail is the answer.

    What the mint after it got is dropped, as if it had never been asked, and
    the claim is released once.
    """
    store = _ClaimStore()
    github = _GitHub(clone, sandbox)

    response = await _claim(store, github)

    assert sorted(github.asked) == ["clone", "sandbox"], (
        "every mint is asked, whatever the other says"
    )
    assert store.failed == []
    decisive = next((outcome for outcome in (clone, sandbox) if outcome != "ok"), None)
    if decisive == "error":
        assert _answer(response)[0] == _GITHUB_ERROR[0]
        assert _answer(response)[1].startswith(_GITHUB_ERROR[1])
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.retry_delays == [30]
        return
    # An App that does not cover the repository is a settled answer: the
    # token minted before it goes to the runner, and none after it.
    assert response.status_code == 200, response.text
    assert store.released == []
    assert response.json()["clone_token"] == ("ghs_clone" if clone == "ok" else None)
    assert response.json()["github_token"] == (
        "ghs_sandbox" if clone == "ok" and sandbox == "ok" else None
    )


async def test_the_two_mints_overlap():
    """The claim waits about as long as its slower mint, not their sum."""
    delay_s = 0.5
    windows: dict[str, tuple[float, float]] = {}

    class SlowGitHub(_GitHub):
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            started = time.monotonic()
            await asyncio.sleep(delay_s)
            token = await super().token_for(repo, **kwargs)
            windows[token] = (started, time.monotonic())
            return token

    app = _app(_ClaimStore())
    app.dependency_overrides[get_agent_app_credentials] = SlowGitHub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Timed around the request alone: building the app is no part of it.
        started = time.monotonic()
        response = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
        elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert sorted(windows) == ["ghs_clone", "ghs_sandbox"]
    # Both mints began before either had finished: neither waited for the other.
    assert max(start for start, _ in windows.values()) < min(end for _, end in windows.values())
    assert elapsed < 1.75 * delay_s, f"took {elapsed:.2f}s; one at a time takes {2 * delay_s}s"


async def test_a_workspace_without_a_repository_asks_github_nothing():
    github = _GitHub()
    claim = {"id": _JOB, "attempt_id": _ATTEMPT, "repo": None, "thread_id": "athr_1"}

    assert await mint_claim_credentials(_ClaimStore(), claim, app_credentials=github) == (
        None,
        None,
    )
    assert github.asked == []


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


async def test_a_cancelled_claim_waits_for_every_mint_and_settles_nothing():
    """A cancellation reaches both mints, and the claim is neither released nor ended."""
    held = _Held(["clone", "sandbox"])

    class HeldGitHub:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            scope = "clone" if kwargs["permissions"] == {"contents": "read"} else "sandbox"
            return await held.hold(scope)

    store = _ClaimStore()
    claim = await store.claim_job(
        worker_id="w1", lease_ttl_seconds=60, host="runner-test", host_credential_hash="hash"
    )
    assert claim is not None
    minting = asyncio.create_task(
        mint_claim_credentials(store, claim, app_credentials=HeldGitHub())
    )
    await asyncio.wait_for(held.all_started.wait(), 5)

    minting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await minting

    assert sorted(held.ended) == sorted(held.names), "a mint outlived the claim"
    assert store.released == []
    assert store.failed == []
