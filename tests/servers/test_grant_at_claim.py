"""The claim asks the gateway for the sandbox's credential (task E9).

Before this the claim minted a ``model``-scoped worker token signed with
``AGENT_CONTROL_TOKEN_SECRET`` — a root the gateway does not know. It parsed,
it looked like a credential, and it bought nothing: the sandbox's first model
call was going to be refused whatever happened here. The claim now asks for a
grant instead, and these cases pin the three things that has to get right.

**Scope.** The grant names the one model the job declared and the MCP servers
it was created with, not everything the owner's role could reach. A grant is
the only credential inside the sandbox; anything it carries that the job does
not need is reachable by whatever the agent runs.

**Failure hands the job back.** A grant that could not be minted means an agent
that cannot call a model, so starting the attempt would burn one of the job's
three tries on a run that fails at its first request. Releasing the claim costs
nothing — a released claim is not a spent attempt.

**The link is recorded under the fence.** Renewal and revocation are both
lookups through ``agent_attempts.grant_id``; a grant recorded against an
attempt that was superseded in the meantime would be a grant renewed forever.

**And a grant that cannot be linked is withdrawn, not handed over.** The link
is the only thing that will ever renew or revoke a capability, so an unlinked
one is live, unowned and unfindable until its TTL runs out. Both ways it can
happen — no ``grant_id`` in the answer, and a fenced UPDATE that matched
nothing — are pinned below.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import routes as agent_jobs_router
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_agent_job_store,
    get_grant_client,
)
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio

_JOB = "ajob_0001"
_ATTEMPT = 100
_GENERATION = 1


class _ClaimStore:
    """Only the surface ``worker_claim`` touches."""

    def __init__(self, *, queued: bool = True, fence_still_current: bool = True) -> None:
        self.queued = queued
        self.fence_still_current = fence_still_current
        self.released: list[tuple[str, int]] = []
        self.grants: dict[int, str] = {}

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        host: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.queued:
            return None
        self.queued = False
        return {
            "id": _JOB,
            "thread_id": "athr_1",
            "parent_job_id": None,
            "turn_no": 1,
            "attempt_id": _ATTEMPT,
            "attempt_no": 1,
            "lease_generation": _GENERATION,
            "user_id": "user-owner",
            "repo": "owner/name",
            "base_sha": "a" * 40,
            "task_prompt": "fix it",
            "setup_script": None,
            "runtime": "claude-code",
            "model": "glm-5.1",
            "mcp_servers": ["docs"],
            "metadata": {},
        }

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        self.released.append((job_id, attempt_id))
        return True

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        if not self.fence_still_current:
            return False
        self.grants[attempt_id] = grant_id
        return True


class _Grants:
    """The grant client, answering or refusing on demand."""

    def __init__(
        self,
        *,
        fail: bool = False,
        grant_id: str | None = "agr_7",
        granted_models: list[str] | None = None,
    ) -> None:
        self.fail = fail
        self.grant_id = grant_id
        #: What the gateway *actually* granted, when that differs from what was
        #: asked. ``None`` means "everything requested", which is the ordinary
        #: case; a narrower list models the user's role changing while the job
        #: sat in the queue, which is the whole reason the claim checks.
        self.granted_models = granted_models
        self.calls: list[dict[str, Any]] = []
        self.revoked: list[str] = []

    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise GatewayError("gateway answered 503 for /internal/agent-grants")
        minted: dict[str, Any] = {
            "token": "agr.header.signature",
            # Default: the gateway granted what was asked. `granted_models`
            # overrides it, which is how a role change mid-queue is modelled.
            "allowed_models": (
                list(kwargs.get("models") or [])
                if self.granted_models is None
                else list(self.granted_models)
            ),
        }
        if self.grant_id is not None:
            minted["grant_id"] = self.grant_id
        return minted

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.append(grant_id)
        return True


def _app(store: _ClaimStore, grants: _Grants) -> FastAPI:
    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_grant_client] = lambda: grants
    app.dependency_overrides[agent_jobs_router.verify_claiming_host] = lambda: {
        "host": "runner-test",
        "credential_hash": "test-credential-hash",
    }
    return app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "claim-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "claim-test-broker-secret")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    yield


@pytest_asyncio.fixture()
async def claim():
    """Return a helper that drives one claim against a fresh app."""

    async def _drive(store: _ClaimStore, grants: _Grants):
        transport = ASGITransport(app=_app(store, grants))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    return _drive


async def test_the_sandbox_credential_is_the_gateways_grant(claim):
    store, grants = _ClaimStore(), _Grants()

    response = await claim(store, grants)

    assert response.status_code == 200
    assert response.json()["sandbox_token"] == "agr.header.signature"
    # And the runner's own token is still this service's own, unchanged.
    from cloud_agent.tokens import SCOPE_FULL, parse_worker_token

    assert parse_worker_token(response.json()["worker_token"])["scope"] == SCOPE_FULL


async def test_the_grant_is_scoped_to_the_job(claim):
    """Not to everything the owner could reach: this token lives in a sandbox
    running code nobody in this repository wrote."""
    store, grants = _ClaimStore(), _Grants()

    await claim(store, grants)

    assert grants.calls == [
        {
            "user_id": "user-owner",
            "job_id": _JOB,
            "attempt_id": _ATTEMPT,
            "models": ["glm-5.1"],
            # No MCP scope. The claim still resolves servers for the sandbox —
            # that is `mcp_servers` on the claim response — but it is not
            # something the gateway is asked to scope, and this assertion said
            # it was right up until the request stopped carrying it.
        }
    ]


async def test_the_grant_is_linked_to_the_attempt_under_its_fence(claim):
    store, grants = _ClaimStore(), _Grants()

    await claim(store, grants)

    assert store.grants == {_ATTEMPT: "agr_7"}


async def test_a_refused_grant_returns_the_job_to_the_queue(claim):
    """Fail closed, and spend nothing doing it.

    503 rather than 500: the runner should idle and claim again, exactly as it
    does when GitHub cannot mint a clone credential.
    """
    store, grants = _ClaimStore(), _Grants(fail=True)

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "credential_unavailable"
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.grants == {}


async def test_a_grant_that_cannot_be_linked_is_revoked_not_handed_over(claim):
    """The fence moved between the mint and the write.

    Handing the token over anyway would put a live capability inside a sandbox
    that nothing will renew and nothing will revoke: it would survive to its
    TTL with no owner, and no operator could find it from either side.
    """
    store, grants = _ClaimStore(fence_still_current=False), _Grants()

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "credential_unavailable"
    assert grants.revoked == ["agr_7"]
    assert store.grants == {}


async def test_a_grant_with_no_id_is_refused_rather_than_used(claim):
    """A token this service cannot name is a token it cannot ever withdraw.

    Nothing can be revoked here — there is no id to revoke — so the only
    remaining protection is refusing to run with it.
    """
    store, grants = _ClaimStore(), _Grants(grant_id=None)

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "credential_unavailable"
    assert grants.revoked == []
    assert store.released == [(_JOB, _ATTEMPT)]


async def test_an_empty_queue_never_asks_for_a_grant(claim):
    """No work, no credential — and no gateway call to be rate-limited by."""
    store, grants = _ClaimStore(queued=False), _Grants()

    response = await claim(store, grants)

    assert response.status_code == 200
    assert response.json() is None
    assert grants.calls == []


async def test_a_grant_that_omits_the_job_model_is_refused(claim):
    """Asked for is not granted.

    The gateway clamps the requested models against the role the user has
    *now*, and a job can sit in the queue long enough for a downgrade or for a
    model to leave that tier. The clamp is not an error there — the response
    reports the effective list and the request was satisfiable, just not on
    the caller's terms.

    Taken at face value, that produces a sandbox which starts, spends an
    attempt, and fails on its first inference call with an authorization error
    the user cannot act on. The claim has the effective list in its hand; the
    only question was whether it read it.
    """
    store, grants = _ClaimStore(), _Grants(granted_models=["some-other-model"])

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "model_not_granted"
    # Withdrawn, not left live: nothing will ever renew a grant whose claim
    # failed, but its TTL would otherwise keep it usable for minutes.
    assert grants.revoked == ["agr_7"]
    assert store.released == [(_JOB, _ATTEMPT)]


async def test_a_grant_with_no_scope_at_all_is_refused(claim):
    """Absence is refusal.

    The contract says the gateway returns this field. If it does not, the
    scope cannot be verified — and "cannot verify" is not a reason to hand a
    capability to a sandbox running someone else's repository.
    """
    store, grants = _ClaimStore(), _Grants(granted_models=[])

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "model_not_granted"
    assert grants.revoked == ["agr_7"]


@pytest.mark.parametrize("pending", [True, False])
async def test_title_generation_is_scheduled_after_successful_claim(monkeypatch, pending):
    """Claim returns before title inference and never gives its token extra scope."""
    from unittest.mock import AsyncMock

    from fastapi import BackgroundTasks

    from cloud_agent.schemas import WorkerClaimRequest

    store, grants = _ClaimStore(), _Grants()
    original_claim = store.claim_job

    async def claim_with_title_state(**kwargs):
        result = await original_claim(**kwargs)
        result["title_generation_pending"] = pending
        return result

    monkeypatch.setattr(store, "claim_job", claim_with_title_state)
    generate = AsyncMock()
    monkeypatch.setattr(agent_jobs_router, "generate_thread_title", generate)
    background = BackgroundTasks()
    response = await agent_jobs_router.worker_claim(
        WorkerClaimRequest(worker_id="w1"),
        background,
        claiming_host={"host": "runner-test", "credential_hash": "test-credential-hash"},
        store=store,
        app_credentials=None,
        grants=grants,
    )
    assert response.job_id == _JOB
    generate.assert_not_awaited()
    assert len(background.tasks) == int(pending)
    await background()
    if pending:
        generate.assert_awaited_once_with(
            store,
            job_id=_JOB,
            attempt_id=_ATTEMPT,
            lease_generation=_GENERATION,
            prompt="fix it",
            model="glm-5.1",
            sandbox_token="agr.header.signature",
        )
    else:
        generate.assert_not_awaited()


async def test_failed_claim_does_not_schedule_title_generation(monkeypatch, claim):
    from unittest.mock import AsyncMock

    generate = AsyncMock()
    monkeypatch.setattr(agent_jobs_router, "generate_thread_title", generate)
    store, grants = _ClaimStore(), _Grants(fail=True)
    original_claim = store.claim_job

    async def claim_with_title_state(**kwargs):
        result = await original_claim(**kwargs)
        result["title_generation_pending"] = True
        return result

    monkeypatch.setattr(store, "claim_job", claim_with_title_state)
    response = await claim(store, grants)
    assert response.status_code == 503
    generate.assert_not_awaited()
