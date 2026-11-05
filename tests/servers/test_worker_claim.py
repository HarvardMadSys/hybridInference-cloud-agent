"""What a claim checks before a host gets a turn, and what it hands over.

A claim is where queued work meets the world as it is now. Every turn passes
these fences here, including the ones no request admitted (a goal's round, a
waiting message): the owner's access, the repository, the environment and its
capabilities, and the turn's model connection. Each refusal is decided before
any key is read, either ending the turn with a reason its owner can read or
handing it back with no attempt spent.

**The connection comes last.** Nothing below reads the owner's provider key
until the turn is known to be runnable, which ``connection_reads`` pins.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.owners import CONNECTION_ID, connection_claim, connection_row
from tests.support.storage import AllCapabilities, EmptyAttachments

from cloud_agent.accounts.directory import OwnerDirectoryError
from cloud_agent.api import routes as agent_jobs_router, worker as agent_worker
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_owner_directory,
    get_storage,
)

pytestmark = pytest.mark.asyncio

_JOB = "ajob_0001"
_ATTEMPT = 100
_GENERATION = 1
_KEY_SECRET = "claim-test-connection-encryption-key"


class _ClaimStore(EmptyAttachments, AllCapabilities):
    """Only the surface ``worker_claim`` touches."""

    def __init__(self, *, queued: bool = True, connection_id: str | None = CONNECTION_ID) -> None:
        self.queued = queued
        self.connection_id = connection_id
        self.released: list[tuple[str, int]] = []
        self.retry_delays: list[float] = []
        self.release_reasons: list[str] = []
        self.failed: list[str] = []
        self.failure_details: list[str | None] = []
        #: Which connections were read, and so whose key could have been touched.
        self.connection_reads: list[str] = []

    async def get_model_connection(self, *, user_id: str, connection_id: str):
        self.connection_reads.append(connection_id)
        return connection_row(user_id, connection_id=connection_id)

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
            "model_connection_id": self.connection_id,
            "mcp_servers": ["docs"],
            "metadata": {},
        }

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        """This fixture has no account MCP connections."""
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
        assert to_state == "failed"
        self.failed.append(job_id)
        self.failure_details.append(kwargs.get("detail"))
        return True


class _Owners:
    """The account's Agent access decision, as the claim re-reads it.

    Allowed unless told otherwise. ``decision`` replaces the answer; ``error``
    makes the lookup itself fail, which is how both a withdrawn account
    (``subject_unavailable``) and an unreadable store arrive.
    """

    def __init__(
        self,
        *,
        decision: dict[str, Any] | None = None,
        error: OwnerDirectoryError | None = None,
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


def _app(store: _ClaimStore, owners: _Owners | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    # Every claim asks the directory again whether the owner may use Agent.
    resolved = owners if owners is not None else _Owners()
    app.dependency_overrides[get_owner_directory] = lambda: resolved
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
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _KEY_SECRET)
    yield


@pytest_asyncio.fixture()
async def claim():
    """Return a helper that drives one claim against a fresh app."""

    async def _drive(store: _ClaimStore, owners: _Owners | None = None):
        transport = ASGITransport(app=_app(store, owners))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
            )

    return _drive


async def test_the_claim_hands_over_the_turns_connection_and_the_runners_own_token(claim):
    store = _ClaimStore()

    response = await claim(store)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_connection"]["connection_id"] == CONNECTION_ID
    assert "sandbox_token" not in body
    assert body["plugins"] == []
    assert store.connection_reads == [CONNECTION_ID]
    # And the runner's own token is still this service's own, unchanged.
    from cloud_agent.tokens import SCOPE_FULL, parse_worker_token

    assert parse_worker_token(body["worker_token"])["scope"] == SCOPE_FULL


async def test_a_turn_with_no_connection_ends_with_no_other_credential(claim):
    """There is nothing else to run it on, and handing it back would only repeat this."""
    store = _ClaimStore(connection_id=None)

    response = await claim(store)

    assert response.status_code == 403
    error = response.json()["detail"]["error"]
    assert (error["type"], error["reason"]) == ("model_connection_unusable", "no_connection")
    assert "worker_token" not in response.json()
    assert store.failed == [_JOB]
    assert store.released == []
    assert store.connection_reads == []


async def test_a_turn_whose_connection_was_deleted_ends_with_that_reason(claim, monkeypatch):
    store = _ClaimStore()

    async def deleted(*, user_id: str, connection_id: str):
        store.connection_reads.append(connection_id)
        return None

    monkeypatch.setattr(store, "get_model_connection", deleted)
    response = await claim(store)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["reason"] == "not_found"
    assert store.failed == [_JOB]
    assert store.released == []


async def test_a_host_that_cannot_launch_on_a_connection_gets_the_claim_back(monkeypatch):
    """Not the owner's to fix: no attempt spent, no key read."""
    store = _ClaimStore()
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert response.status_code == 503, response.text
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.retry_delays == [30]
    assert store.failed == []
    assert store.connection_reads == []


async def test_a_deployment_that_cannot_read_the_key_hands_the_claim_back(claim, monkeypatch):
    """The key's cipher is the operator's problem, never a verdict on the owner's turn."""
    from cloud_agent.credential_cipher import CredentialCipherError

    store = _ClaimStore()

    def unreadable() -> Any:
        raise CredentialCipherError("no key")

    monkeypatch.setattr(
        "cloud_agent.application.model_connections.model_connection_cipher", unreadable
    )
    response = await claim(store)

    assert response.status_code == 503, response.text
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.failed == []


async def test_an_empty_queue_never_reads_a_connection(claim):
    """No work, no credential."""
    store = _ClaimStore(queued=False)

    response = await claim(store)

    assert response.status_code == 200
    assert response.json() is None
    assert store.connection_reads == []


async def test_claim_delivers_the_threads_plugins(claim, monkeypatch):
    store = _ClaimStore()
    plugins = [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]
    original = store.claim_job

    async def claim_with_plugins(**kwargs):
        job = await original(**kwargs)
        assert job is not None
        return {**job, "plugins": plugins}

    monkeypatch.setattr(store, "claim_job", claim_with_plugins)
    response = await claim(store)

    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == plugins


async def test_an_unavailable_repository_lookup_delays_the_claim(claim, monkeypatch):
    from cloud_agent.entitlement import RepoAccessUnavailable

    async def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RepoAccessUnavailable("GitHub is temporarily unavailable")

    monkeypatch.setattr(agent_worker, "require_entitled_repo", unavailable)
    store = _ClaimStore()
    response = await claim(store)
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "source_control_unavailable"
    assert store.failed == []
    assert store.retry_delays == [30]


@pytest.mark.parametrize(
    "owners",
    [
        _Owners(decision={"allowed": False, "permissions": []}),
        _Owners(
            error=OwnerDirectoryError(
                "account unavailable", status_code=403, error_type="subject_unavailable"
            )
        ),
    ],
    ids=["denied", "subject_unavailable"],
)
async def test_an_owner_whose_agent_access_was_withdrawn_is_not_run(claim, owners):
    """Queued work does not outlive its owner's access.

    A goal's round or a waiting message is written with no request to admit
    it, and any job can wait in the queue past a withdrawal. So the claim asks, and a "no" ends the job with the
    reason rather than handing it back to be refused again at every claim.
    """
    store = _ClaimStore()

    response = await claim(store, owners)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "agent_access_denied"
    assert owners.asked == ["user-owner"]
    assert store.connection_reads == []
    assert store.failed == [_JOB]
    assert store.released == []


@pytest.mark.parametrize(
    "owners",
    [
        _Owners(error=OwnerDirectoryError("account store unreachable")),
        _Owners(error=OwnerDirectoryError("account store unavailable", status_code=503)),
        _Owners(decision={"allowed": True, "permissions": []}),
    ],
    ids=["unreachable", "unavailable", "malformed"],
)
async def test_an_unconfirmed_agent_access_hands_the_job_back(claim, owners):
    """No answer is not permission, and not a verdict on the owner either."""
    store = _ClaimStore()

    response = await claim(store, owners)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "agent_access_unavailable"
    assert store.connection_reads == []
    assert store.failed == []
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.retry_delays == [30]


@pytest.mark.parametrize("refusal", ["not_allowed", "unavailable"])
async def test_a_repository_refusal_is_decided_before_any_credential(claim, monkeypatch, refusal):
    """A job that cannot run reads no key: ended or delayed, no credential is in play."""
    from cloud_agent.entitlement import RepoAccessUnavailable, RepoNotAllowed

    async def refused(*args: Any, **kwargs: Any) -> None:
        if refusal == "not_allowed":
            raise RepoNotAllowed("owner/name is no longer reachable by its owner")
        raise RepoAccessUnavailable("GitHub is temporarily unavailable")

    monkeypatch.setattr(agent_worker, "require_entitled_repo", refused)
    store = _ClaimStore()

    response = await claim(store)

    assert response.status_code == (403 if refusal == "not_allowed" else 503)
    assert store.connection_reads == []
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
    store = _ClaimStore()

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

    response = await claim(store)

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "claim_failed"
    assert store.released == [(_JOB, _ATTEMPT)]
    assert store.release_reasons == ["claim_failed"]
    assert store.retry_delays == [30]
    assert store.failed == []


@pytest.mark.parametrize("compatibility", ["old-runner", "missing-broker", "old-broker", "ready"])
async def test_environment_claim_requires_both_actual_runner_and_broker(monkeypatch, compatibility):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent.sandbox_manager_client import SandboxManagerError
    from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable

    store = _ClaimStore()
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
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=connection_claim(body))
    if compatibility == "ready":
        assert response.status_code == 200, response.text
        assert response.json()["environment"]["binding_id"] == "frozen-binding"
        assert response.json()["environment"]["variables"][0]["value"] == "private-example"
        assert store.connection_reads == [CONNECTION_ID]
        assert store.released == []
        broker.require_environment_capability.assert_awaited_once()
    else:
        assert response.status_code == 503, response.text
        assert "private-example" not in response.text
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.retry_delays == [30]
        assert store.connection_reads == []
        assert store.failed == []


async def test_a_team_environment_its_owner_lost_fails_the_turn_before_any_credential(
    monkeypatch,
):
    """Leaving the team ends the chat's use of its Secrets; no host change helps."""
    from unittest.mock import AsyncMock

    from cloud_agent.environments import EnvironmentAccessRevoked

    store = _ClaimStore()
    original_claim = store.claim_job

    async def bound_claim(**kwargs):
        result = await original_claim(**kwargs)
        return {**result, "environment_binding_id": "team-binding"}

    monkeypatch.setattr(store, "claim_job", bound_claim)
    store.environment_for_claim = AsyncMock(
        side_effect=EnvironmentAccessRevoked(
            "This chat uses an environment of a team you are no longer a member of"
        )
    )
    body = {"worker_id": "w1", "attestation": {"runner_capabilities": ["environment-v1"]}}
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=connection_claim(body))

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"]["type"] == "team_environment_revoked"
    assert store.failed == [_JOB]
    assert store.released == []
    assert store.connection_reads == []


@pytest.mark.parametrize("snapshot_capable", [False, True])
async def test_snapshot_fields_and_a_snapshot_go_only_to_a_runner_that_reads_them(
    monkeypatch, snapshot_capable
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent_shared.environment import BoundEnvironment

    image = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
    store = _ClaimStore()
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
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=connection_claim(body))
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

    store = _ClaimStore()
    store.environment_snapshots = SimpleNamespace(record_use=AsyncMock(return_value=True))
    token = mint_worker_token(
        job_id=_JOB, attempt_id=_ATTEMPT, lease_generation=_GENERATION, scope=SCOPE_FULL
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
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
    # Fenced by the store to the attempt the token names.
    store.environment_snapshots.record_use.assert_awaited_once_with(
        job_id=_JOB,
        attempt_id=_ATTEMPT,
        lease_generation=_GENERATION,
        snapshot_id=None,
        outcome="absent",
        base_sha="b" * 40,
    )
    assert refused.status_code == 422


async def test_a_snapshot_report_from_a_token_that_lost_its_lease_is_refused():
    """A worker token names its attempt's fence and never expires.

    So the store asks whether that attempt still holds the job's lease: a
    superseded attempt's token, or one from a finished turn or a removed host,
    could otherwise delete a snapshot whole teams start from, or queue a build
    at any commit, at any later time.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent.tokens import SCOPE_FULL, mint_worker_token

    async def record_use(**report: Any) -> bool:
        return (report.get("attempt_id"), report.get("lease_generation")) == (
            _ATTEMPT,
            _GENERATION,
        )

    store = _ClaimStore()
    store.environment_snapshots = SimpleNamespace(record_use=AsyncMock(side_effect=record_use))
    stale = mint_worker_token(
        job_id=_JOB, attempt_id=_ATTEMPT + 1, lease_generation=_GENERATION + 1, scope=SCOPE_FULL
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/agent/worker/jobs/{_JOB}/environment-snapshot",
            json={"snapshot_id": "a" * 32, "outcome": "invalid", "base_sha": None},
            headers={"Authorization": f"Bearer {stale}"},
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "lease_lost"
    store.environment_snapshots.record_use.assert_awaited_once_with(
        job_id=_JOB,
        attempt_id=_ATTEMPT + 1,
        lease_generation=_GENERATION + 1,
        snapshot_id="a" * 32,
        outcome="invalid",
        base_sha=None,
    )


@pytest.mark.parametrize("inference_capable", [False, True])
async def test_owner_inference_credentials_wait_for_a_runner_that_keeps_them_from_the_cli(
    monkeypatch, inference_capable
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable

    store = _ClaimStore()
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
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=connection_claim(body))
    if inference_capable:
        assert response.status_code == 200, response.text
        # The broker is asked for the capabilities this binding needs.
        broker.require_environment_capability.assert_awaited_once_with(bound)
    else:
        assert response.status_code == 503, response.text
        assert owner_value not in response.text
        assert store.released == [(_JOB, _ATTEMPT)]
        assert store.connection_reads == []


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
    store = _ClaimStore()
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
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=connection_claim(body))
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
        assert store.connection_reads == []


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
    store = _ClaimStore()
    store.ssh_group_state = AsyncMock(
        return_value={
            "revision": 9,
            "sandbox_id": "asbx_1",
            "members": [{"thread_id": "athr_1", "slot_id": 2, "servers": [server]}, *forks],
        }
    )
    body = {"worker_id": "w1", "attestation": {"runner_capabilities": ["ssh-access-v1"]}}
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/worker/claim", json=connection_claim(body))

    assert response.status_code == 200, response.text
    assert store.released == []
    access = response.json()["ssh_access"]
    assert [(member["thread_id"], member["slot_id"]) for member in access["members"]] == [
        ("athr_1", 2)
    ]
    assert access["members"][0]["servers"][0]["private_key"] == private
