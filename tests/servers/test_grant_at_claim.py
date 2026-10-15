"""The claim asks the gateway for the sandbox's credential.

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
from tests.support.storage import AllCapabilities, EmptyAttachments

from cloud_agent.api import routes as agent_jobs_router, worker as agent_worker
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_grant_client,
    get_storage,
)
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio

_JOB = "ajob_0001"
_ATTEMPT = 100
_GENERATION = 1


class _ClaimStore(EmptyAttachments, AllCapabilities):
    """Only the surface ``worker_claim`` touches."""

    def __init__(self, *, queued: bool = True, fence_still_current: bool = True) -> None:
        self.queued = queued
        self.fence_still_current = fence_still_current
        self.released: list[tuple[str, int]] = []
        self.retry_delays: list[float] = []
        self.release_reasons: list[str] = []
        self.failed: list[str] = []
        # Named apart from the capability: `store.linked` is now the grant
        # repository the route reaches for, not this fake's record of what it
        # linked.
        self.linked: dict[int, str] = {}

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

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        """This grant-focused fixture has no account MCP connections."""
        return []

    async def release_claim(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int | None = None,
        reason: str = "credential_unavailable",
        retry_after_seconds: float = 0,
    ) -> bool:
        self.released.append((job_id, attempt_id))
        self.release_reasons.append(reason)
        self.retry_delays.append(retry_after_seconds)
        return True

    async def transition(self, *, job_id: str, to_state: str, **kwargs: Any) -> bool:
        if not self.fence_still_current:
            return False
        assert to_state == "failed"
        self.failed.append(job_id)
        return True

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        if not self.fence_still_current:
            return False
        self.linked[attempt_id] = grant_id
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


class _Gateway:
    """The Gateway's Agent access decision, as the claim re-reads it.

    Allowed unless told otherwise. ``decision`` replaces the answer; ``error``
    makes the lookup itself fail, which is how both a withdrawn account
    (``subject_unavailable``) and an unreachable Gateway arrive.
    """

    def __init__(
        self,
        *,
        decision: dict[str, Any] | None = None,
        error: GatewayError | None = None,
    ) -> None:
        self.decision = decision
        self.error = error
        self.asked: list[str] = []

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error is not None:
            raise self.error
        if self.decision is not None:
            return {"user_id": user_id, **self.decision}
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}


def _app(store: _ClaimStore, grants: _Grants, gateway: _Gateway | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_grant_client] = lambda: grants
    # Every claim asks the Gateway again whether the owner may use Agent.
    resolved_gateway = gateway if gateway is not None else _Gateway()
    app.dependency_overrides[get_gateway_client] = lambda: resolved_gateway
    app.dependency_overrides[agent_worker.verify_claiming_host] = lambda: {
        "host": "runner-test",
        "credential_hash": "test-credential-hash",
    }
    return app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "claim-test-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "claim-test-sandbox-manager-secret")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    yield


@pytest_asyncio.fixture()
async def claim():
    """Return a helper that drives one claim against a fresh app."""

    async def _drive(store: _ClaimStore, grants: _Grants, gateway: _Gateway | None = None):
        transport = ASGITransport(app=_app(store, grants, gateway))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    return _drive


async def test_the_sandbox_credential_is_the_gateways_grant(claim):
    store, grants = _ClaimStore(), _Grants()

    response = await claim(store, grants)

    assert response.status_code == 200
    assert response.json()["sandbox_token"] == "agr.header.signature"
    assert response.json()["plugins"] == []
    # And the runner's own token is still this service's own, unchanged.
    from cloud_agent.tokens import SCOPE_FULL, parse_worker_token

    assert parse_worker_token(response.json()["worker_token"])["scope"] == SCOPE_FULL


async def test_claim_delivers_the_threads_plugins(claim, monkeypatch):
    store, grants = _ClaimStore(), _Grants()
    plugins = [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]
    original = store.claim_job

    async def claim_with_plugins(**kwargs):
        job = await original(**kwargs)
        assert job is not None
        return {**job, "plugins": plugins}

    monkeypatch.setattr(store, "claim_job", claim_with_plugins)
    response = await claim(store, grants)

    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == plugins


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

    assert store.linked == {_ATTEMPT: "agr_7"}


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
    assert store.retry_delays == [30]
    assert store.linked == {}


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
    assert store.linked == {}


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

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "model_not_granted"
    # Withdrawn, not left live: nothing will ever renew a grant whose claim
    # failed, but its TTL would otherwise keep it usable for minutes.
    assert grants.revoked == ["agr_7"]
    assert store.released == []
    assert store.failed == [_JOB]


async def test_a_grant_with_no_scope_at_all_is_refused(claim):
    """Absence is refusal.

    The contract says the gateway returns this field. If it does not, the
    scope cannot be verified — and "cannot verify" is not a reason to hand a
    capability to a sandbox running someone else's repository.
    """
    store, grants = _ClaimStore(), _Grants(granted_models=[])

    response = await claim(store, grants)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "model_not_granted"
    assert grants.revoked == ["agr_7"]
    assert store.failed == [_JOB]


async def test_an_unreadable_grant_scope_retries_without_ending_the_job(claim):
    class UnreadableGrants(_Grants):
        async def mint(self, **kwargs: Any) -> dict[str, Any]:
            grant = await super().mint(**kwargs)
            grant.pop("allowed_models")
            return grant

    store, grants = _ClaimStore(), UnreadableGrants()
    response = await claim(store, grants)
    assert response.status_code == 503
    assert grants.revoked == ["agr_7"]
    assert store.failed == []
    assert store.retry_delays == [30]


async def test_a_definitively_unavailable_subject_ends_the_job(claim):
    class UnavailableSubjectGrants(_Grants):
        async def mint(self, **kwargs: Any) -> dict[str, Any]:
            raise GatewayError(
                "subject unavailable", status_code=403, error_type="subject_unavailable"
            )

    store = _ClaimStore()
    response = await claim(store, UnavailableSubjectGrants())
    assert response.status_code == 403
    assert store.failed == [_JOB]
    assert store.released == []


async def test_an_unavailable_repository_lookup_delays_the_claim(claim, monkeypatch):
    from cloud_agent.entitlement import RepoAccessUnavailable

    async def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RepoAccessUnavailable("GitHub is temporarily unavailable")

    monkeypatch.setattr(agent_worker, "require_entitled_repo", unavailable)
    store = _ClaimStore()
    response = await claim(store, _Grants())
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "source_control_unavailable"
    assert store.failed == []
    assert store.retry_delays == [30]


@pytest.mark.parametrize(
    "gateway",
    [
        _Gateway(decision={"allowed": False, "permissions": []}),
        _Gateway(
            error=GatewayError(
                "gateway answered 403", status_code=403, error_type="subject_unavailable"
            )
        ),
    ],
    ids=["denied", "subject_unavailable"],
)
async def test_an_owner_whose_agent_access_was_withdrawn_is_not_run(claim, gateway):
    """Queued work does not outlive its owner's access.

    A goal's round or a waiting message is written with no request to admit
    it, and any job can wait in the queue past a withdrawal. The grant mint
    alone would not notice — the Gateway refuses it only for an unknown or
    inactive account — so the claim asks, and a "no" ends the job with the
    reason rather than handing it back to be refused again at every claim.
    """
    store, grants = _ClaimStore(), _Grants()

    response = await claim(store, grants, gateway)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "agent_access_denied"
    assert gateway.asked == ["user-owner"]
    assert grants.calls == []
    assert store.failed == [_JOB]
    assert store.released == []


@pytest.mark.parametrize(
    "gateway",
    [
        _Gateway(error=GatewayError("gateway unreachable")),
        _Gateway(error=GatewayError("gateway answered 503", status_code=503)),
        _Gateway(decision={"allowed": True, "permissions": []}),
    ],
    ids=["unreachable", "unavailable", "malformed"],
)
async def test_an_unconfirmed_agent_access_hands_the_job_back(claim, gateway):
    """No answer is not permission, and not a verdict on the owner either."""
    store, grants = _ClaimStore(), _Grants()

    response = await claim(store, grants, gateway)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "agent_access_unavailable"
    assert grants.calls == []
    assert store.failed == []
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.retry_delays == [30]


@pytest.mark.parametrize("refusal", ["not_allowed", "unavailable"])
async def test_a_repository_refusal_is_decided_before_any_grant_is_minted(
    claim, monkeypatch, refusal
):
    """A job that cannot run asks the gateway for nothing.

    The entitlement re-check used to follow the mint, so each claim of a job
    whose owner could no longer reach its repository cost the gateway a grant
    and, a reaper pass later, a revocation — for a turn that was never going to
    start. Ended or delayed, the job is decided with no credential in play.
    """
    from cloud_agent.entitlement import RepoAccessUnavailable, RepoNotAllowed

    async def refused(*args: Any, **kwargs: Any) -> None:
        if refusal == "not_allowed":
            raise RepoNotAllowed("owner/name is no longer reachable by its owner")
        raise RepoAccessUnavailable("GitHub is temporarily unavailable")

    monkeypatch.setattr(agent_worker, "require_entitled_repo", refused)
    store, grants = _ClaimStore(), _Grants()

    response = await claim(store, grants)

    assert response.status_code == (403 if refusal == "not_allowed" else 503)
    assert grants.calls == []
    assert grants.revoked == []
    assert store.linked == {}
    if refusal == "not_allowed":
        assert response.json()["detail"]["error"]["type"] == "repo_not_allowed"
        assert store.failed == [_JOB]
        assert store.released == []
    else:
        assert store.failed == []
        assert store.retry_delays == [30]


@pytest.mark.parametrize("failing_step", ["mcp_bindings", "attachment_count", "response"])
async def test_an_unexpected_failure_after_the_claim_hands_the_job_back(
    claim, monkeypatch, failing_step
):
    """A claim that cannot be assembled must not be left holding the job.

    Every refusal above releases its claim, but anything else that raised
    between the claim and the answer — the MCP bindings read, the upload
    count, building the response — used to escape as a 500 with the claim
    still held. The job then waited out a lease nothing was using, and the
    reaper charged it a retry for a turn no agent ever started.
    """
    store, grants = _ClaimStore(), _Grants()

    async def broken_read(**_kwargs: Any) -> Any:
        raise RuntimeError("the store dropped the connection")

    if failing_step == "mcp_bindings":
        monkeypatch.setattr(agent_worker, "resolve_for_thread", broken_read)
    elif failing_step == "attachment_count":
        monkeypatch.setattr(store, "count_thread_attachments", broken_read)
    else:
        original_claim = store.claim_job

        async def claim_the_response_refuses(**kwargs: Any) -> dict[str, Any] | None:
            job = await original_claim(**kwargs)
            assert job is not None
            # A stored value the response model will not accept.
            return {**job, "metadata": ["not", "a", "mapping"]}

        monkeypatch.setattr(store, "claim_job", claim_the_response_refuses)

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "claim_failed"
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.release_reasons == ["claim_failed"]
    assert store.retry_delays == [30]
    assert store.failed == []


async def test_a_grant_whose_link_raises_is_revoked_and_the_job_handed_back(claim):
    """A minted grant the store could not record is withdrawn at once.

    The link is the only thing that will ever renew or revoke it, so a write
    that raised — rather than answering "fenced out" — would otherwise leave a
    live capability that nothing owns until its TTL.
    """

    class BrokenLinkStore(_ClaimStore):
        async def record_attempt_grant(
            self, *, attempt_id: int, lease_generation: int, grant_id: str
        ) -> bool:
            raise RuntimeError("the store dropped the connection")

    store, grants = BrokenLinkStore(), _Grants()

    response = await claim(store, grants)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "claim_failed"
    assert grants.revoked == ["agr_7"]
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.linked == {}


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
    monkeypatch.setattr(agent_worker, "generate_thread_title", generate)
    background = BackgroundTasks()
    response = await agent_worker.worker_claim(
        WorkerClaimRequest(worker_id="w1"),
        background,
        claiming_host={"host": "runner-test", "credential_hash": "test-credential-hash"},
        store=store,
        app_credentials=None,
        grants=grants,
        gateway=_Gateway(),
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
            model="glm-5.1",
            sandbox_token="agr.header.signature",
        )
    else:
        generate.assert_not_awaited()


async def test_failed_claim_does_not_schedule_title_generation(monkeypatch, claim):
    from unittest.mock import AsyncMock

    generate = AsyncMock()
    monkeypatch.setattr(agent_worker, "generate_thread_title", generate)
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


@pytest.mark.parametrize("compatibility", ["old-runner", "missing-broker", "old-broker", "ready"])
async def test_environment_claim_requires_both_actual_runner_and_broker(monkeypatch, compatibility):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent.sandbox_manager_client import SandboxManagerError
    from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable

    store, grants = _ClaimStore(), _Grants()
    original_claim = store.claim_job

    async def bound_claim(**kwargs):
        result = await original_claim(**kwargs)
        return {**result, "environment_binding_id": "frozen-binding"}

    monkeypatch.setattr(store, "claim_job", bound_claim)
    store.environment_for_claim = AsyncMock(
        return_value=BoundEnvironment(
            binding_id="frozen-binding",
            variables=[EnvironmentVariable(name="TOKEN", value="private-example")],
        )
    )
    broker = SimpleNamespace(require_environment_capability=AsyncMock())
    if compatibility == "old-broker":
        broker.require_environment_capability.side_effect = SandboxManagerError(
            503, "Broker upgrade required"
        )
    route = AsyncMock(return_value=None if compatibility == "missing-broker" else broker)
    monkeypatch.setattr("cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", route)
    body = {
        "worker_id": "w1",
        "attestation": {
            "runner_capabilities": [] if compatibility == "old-runner" else ["environment-v1"]
        },
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, grants)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=body)
    if compatibility == "ready":
        assert response.status_code == 200, response.text
        assert response.json()["environment"]["binding_id"] == "frozen-binding"
        assert response.json()["environment"]["variables"][0]["value"] == "private-example"
        assert len(grants.calls) == 1
        assert store.released == []
        broker.require_environment_capability.assert_awaited_once()
    else:
        assert response.status_code == 503, response.text
        assert "private-example" not in response.text
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.retry_delays == [30]
        assert grants.calls == []
        assert store.failed == []


@pytest.mark.parametrize("snapshot_capable", [False, True])
async def test_snapshot_fields_and_a_snapshot_go_only_to_a_runner_that_reads_them(
    monkeypatch, snapshot_capable
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent_shared.environment import BoundEnvironment

    image = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
    store, grants = _ClaimStore(), _Grants()
    original_claim = store.claim_job

    async def bound_claim(**kwargs):
        result = await original_claim(**kwargs)
        return {**result, "environment_binding_id": "frozen-binding"}

    monkeypatch.setattr(store, "claim_job", bound_claim)
    store.environment_for_claim = AsyncMock(
        return_value=BoundEnvironment(
            binding_id="frozen-binding",
            setup_script="npm ci",
            update_script="npm run generate",
            setup_fingerprint="f" * 64,
        )
    )
    snapshot = {"snapshot_id": "5" * 32}
    store.environment_snapshots = SimpleNamespace(
        snapshot_for_claim=AsyncMock(return_value=snapshot)
    )
    broker = SimpleNamespace(require_environment_capability=AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=broker),
    )
    capabilities = ["environment-v1"] + (["environment-snapshot-v1"] if snapshot_capable else [])
    body = {
        "worker_id": "w1",
        "attestation": {"runner_capabilities": capabilities, "sandbox_image_digest": image},
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, grants)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=body)
    assert response.status_code == 200, response.text
    environment = response.json()["environment"]
    if snapshot_capable:
        assert environment["update_script"] == "npm run generate"
        assert environment["setup_fingerprint"] == "f" * 64
        assert response.json()["environment_snapshot"] == snapshot
        store.environment_snapshots.snapshot_for_claim.assert_awaited_once_with(
            user_id="user-owner", binding_id="frozen-binding", base_image=image
        )
    else:
        # A runner a release behind would refuse both fields.
        assert "update_script" not in environment and "setup_fingerprint" not in environment
        assert response.json()["environment_snapshot"] is None
        store.environment_snapshots.snapshot_for_claim.assert_not_awaited()


async def test_a_runner_reports_what_its_new_workspace_did_with_a_snapshot(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent.tokens import SCOPE_FULL, mint_worker_token

    store, grants = _ClaimStore(), _Grants()
    store.environment_snapshots = SimpleNamespace(record_use=AsyncMock(return_value=True))
    token = mint_worker_token(
        job_id=_JOB, attempt_id=_ATTEMPT, lease_generation=_GENERATION, scope=SCOPE_FULL
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, grants)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/agent/worker/jobs/{_JOB}/environment-snapshot",
            json={"snapshot_id": None, "outcome": "absent", "base_sha": "b" * 40},
            headers={"Authorization": f"Bearer {token}"},
        )
        refused = await client.post(
            f"/v1/agent/worker/jobs/{_JOB}/environment-snapshot",
            json={"snapshot_id": None, "outcome": "something", "base_sha": None},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 204, response.text
    store.environment_snapshots.record_use.assert_awaited_once_with(
        job_id=_JOB, snapshot_id=None, outcome="absent", base_sha="b" * 40
    )
    assert refused.status_code == 422


@pytest.mark.parametrize("inference_capable", [False, True])
async def test_owner_inference_credentials_wait_for_a_runner_that_keeps_them_from_the_cli(
    monkeypatch, inference_capable
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable

    store, grants = _ClaimStore(), _Grants()
    original_claim = store.claim_job

    async def bound_claim(**kwargs):
        result = await original_claim(**kwargs)
        return {**result, "environment_binding_id": "frozen-binding"}

    monkeypatch.setattr(store, "claim_job", bound_claim)
    owner_value = "-".join(["owner", "openai", "placeholder"])
    bound = BoundEnvironment(
        binding_id="frozen-binding",
        variables=[EnvironmentVariable(name="OPENAI_API_KEY", value=owner_value)],
    )
    store.environment_for_claim = AsyncMock(return_value=bound)
    broker = SimpleNamespace(require_environment_capability=AsyncMock())
    route = AsyncMock(return_value=broker)
    monkeypatch.setattr("cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", route)
    capabilities = ["environment-v1"] + (["environment-inference-v1"] if inference_capable else [])
    body = {"worker_id": "w1", "attestation": {"runner_capabilities": capabilities}}
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, grants)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=body)
    if inference_capable:
        assert response.status_code == 200, response.text
        # The broker is asked for the capabilities this binding needs.
        broker.require_environment_capability.assert_awaited_once_with(bound)
    else:
        assert response.status_code == 503, response.text
        assert owner_value not in response.text
        assert store.released == [(_JOB, _ATTEMPT)]
        assert grants.calls == []


@pytest.mark.parametrize("compatibility", ["old-runner", "ready"])
async def test_an_ssh_claim_carries_the_groups_state_only_to_a_capable_runner(
    monkeypatch, compatibility
):
    from unittest.mock import AsyncMock

    from cloud_agent.application.ssh_servers import generate_key_pair, ssh_key_cipher

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "ssh-claim-encryption-key")
    _public, private = generate_key_pair("cloud-agent:gpu-3f9c2a1e")
    from tests.support.ssh_keys import host_key as fresh_host_key

    host_key = fresh_host_key()
    server = {
        "id": "3f9c2a1e-0000-4000-8000-000000000001",
        "name": "gpu",
        "description": "",
        "pinned_ipv4": "93.184.216.34",
        "port": 22,
        "username": "ubuntu",
        "host_key_type": "ssh-ed25519",
        "host_key": host_key,
        "private_key_ciphertext": ssh_key_cipher().encrypt(private),
    }
    store, grants = _ClaimStore(), _Grants()
    store.ssh_group_state = AsyncMock(
        return_value={
            "revision": 9,
            "sandbox_id": "asbx_1",
            "members": [{"thread_id": "athr_1", "slot_id": 2, "servers": [server]}],
        }
    )
    body = {
        "worker_id": "w1",
        "attestation": {
            "runner_capabilities": [] if compatibility == "old-runner" else ["ssh-access-v1"]
        },
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, grants)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=body)
    store.ssh_group_state.assert_awaited_once_with(thread_id="athr_1")
    if compatibility == "ready":
        assert response.status_code == 200, response.text
        access = response.json()["ssh_access"]
        assert access["revision"] == 9
        (member,) = access["members"]
        assert member["slot_id"] == 2
        assert member["servers"][0]["private_key"] == private
        prompt = response.json()["task_prompt"]
        assert '"login": "ubuntu@93.184.216.34:22"' in prompt and prompt.endswith("fix it")
        assert private not in prompt
    else:
        assert response.status_code == 503, response.text
        assert private not in response.text
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.retry_delays == [30]
        assert grants.calls == []


async def test_an_ssh_claim_survives_a_group_that_kept_many_forks(monkeypatch):
    """Every Fork leaves a membership row, deleted or failed, and the state lists them all.

    Only the chats with a usable server change anything on the host, so the
    claim still carries the state when the group has more rows than the
    protocol's member bound.
    """
    from unittest.mock import AsyncMock

    from tests.support.ssh_keys import host_key as fresh_host_key

    from cloud_agent.application.ssh_servers import generate_key_pair, ssh_key_cipher

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "ssh-claim-encryption-key")
    _public, private = generate_key_pair("cloud-agent:gpu-3f9c2a1e")
    server = {
        "id": "3f9c2a1e-0000-4000-8000-000000000001",
        "name": "gpu",
        "description": "",
        "pinned_ipv4": "93.184.216.34",
        "port": 22,
        "username": "ubuntu",
        "host_key_type": "ssh-ed25519",
        "host_key": fresh_host_key(),
        "private_key_ciphertext": ssh_key_cipher().encrypt(private),
    }
    forks = [
        {"thread_id": f"athr_fork_{index:03d}", "slot_id": None, "servers": []}
        for index in range(128)
    ]
    store, grants = _ClaimStore(), _Grants()
    store.ssh_group_state = AsyncMock(
        return_value={
            "revision": 9,
            "sandbox_id": "asbx_1",
            "members": [{"thread_id": "athr_1", "slot_id": 2, "servers": [server]}, *forks],
        }
    )
    body = {"worker_id": "w1", "attestation": {"runner_capabilities": ["ssh-access-v1"]}}
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, grants)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=body)

    assert response.status_code == 200, response.text
    assert store.released == []
    access = response.json()["ssh_access"]
    assert [(member["thread_id"], member["slot_id"]) for member in access["members"]] == [
        ("athr_1", 2)
    ]
    assert access["members"][0]["servers"][0]["private_key"] == private
