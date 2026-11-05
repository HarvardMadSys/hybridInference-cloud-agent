"""The claim asks GitHub for one token: the runner's read-only clone token.

The sandbox's own repository-scoped write token is gone: git and gh there hold
a residency's GitHub relay placeholder instead (``github_credential`` on the
residency). These cases pin what is left: an App that does not cover the
repository is a settled answer, any other GitHub failure hands the claim back,
and the claim never carries a GitHub token for the sandbox.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tests.servers.test_worker_claim import _ATTEMPT, _JOB, _app, _ClaimStore
from tests.support.owners import connection_claim

from cloud_agent.api.deps import get_agent_app_credentials
from cloud_agent.application.claim_credentials import mint_clone_token
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
    """The App's token minter: ``ok`` mints, ``missing`` is no installation, ``error`` fails."""

    def __init__(self, answer: str = "ok") -> None:
        self.answer = answer
        self.asked: list[dict[str, Any]] = []

    async def token_for(self, repo: str, **kwargs: Any) -> str:
        self.asked.append(kwargs)
        if self.answer == "missing":
            raise AppNotInstalled(f"no App installation covers {repo}", status=404)
        if self.answer == "error":
            raise TimeoutError("api.github.com timed out")
        return "ghs_clone"


async def _claim(store: _ClaimStore, github: Any) -> Response:
    app = _app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: github
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )


@pytest.mark.parametrize("answer", ["ok", "missing", "error"])
async def test_the_claim_mints_only_a_read_only_clone_token(answer):
    store = _ClaimStore()
    github = _GitHub(answer)

    response = await _claim(store, github)

    assert github.asked == [{"permissions": {"contents": "read"}, "repository_scoped": True}]
    assert store.failed == []
    if answer == "error":
        body = response.json()["detail"]["error"]["message"]
        assert response.status_code == _GITHUB_ERROR[0]
        assert body.startswith(_GITHUB_ERROR[1])
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.retry_delays == [30]
        return
    assert response.status_code == 200, response.text
    assert store.released == []
    assert response.json()["clone_token"] == ("ghs_clone" if answer == "ok" else None)
    assert "github_token" not in response.json(), "no GitHub token reaches a sandbox"


async def test_a_workspace_without_a_repository_asks_github_nothing():
    github = _GitHub()
    claim = {"id": _JOB, "attempt_id": _ATTEMPT, "repo": None, "thread_id": "athr_1"}
    assert await mint_clone_token(_ClaimStore(), claim, app_credentials=github) is None
    assert github.asked == []
