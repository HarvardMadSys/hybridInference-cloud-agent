"""API-surface tests for the headless on-call door (``api/service_jobs.py``).

Runs the real auth: ``verify_oncall_dispatch`` reads its two environment
variables and falls back to the real ``verify_admin_access``, so what these
cases pin is the door itself — not a stub of it. The store is a minimal
in-memory stand-in exposing only what this router asks of the storage layer;
the owner surface's own concurrency and fence semantics are pinned elsewhere.

The one property every case circles: this door must add **no** authority
beyond "create and read on-call jobs as the one configured service account".
A caller with the dispatch token must not read another tenant's job, must not
choose whose quota a job spends, and must not un-mark an on-call job.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import service_jobs
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_storage,
)
from cloud_agent.schemas import BASE_REF_METADATA_KEY
from tests.support.storage import AllCapabilities

pytestmark = pytest.mark.asyncio

_SERVICE_USER = "user-oncall-svc"
_OTHER_USER = "user-somebody-else"
_DISPATCH_TOKEN = "oncall-dispatch-secret-1"
_ADMIN_TOKEN = "admin-secret-1"

#: Model ids the fake gateway resolves; create fails fast on anything else.
_TEST_MODELS = ("glm-5.2", "glm-5.1")


class FakeOnCallStore(AllCapabilities):
    """In-memory stand-in for the slice of the storage layer this router uses."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._next_job = 1
        self._next_event = 1

    async def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        job = {
            "id": job_id,
            "thread_id": f"athr_{job_id}",
            "parent_job_id": None,
            "turn_no": 1,
            "state": "queued",
            "cancel_requested": False,
            "terminal_resume_pending": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "published_commit_sha": None,
            "detail": None,
            "created_at": None,
            "updated_at": None,
            **kwargs,
        }
        job.setdefault("base_sha", None)
        job.setdefault("metadata", None)
        self.jobs[job_id] = job
        return job

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """The service account holds no MCP connections — the truthful answer."""
        return []

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        job = self.jobs.get(job_id)
        if job is None or (user_id is not None and job["user_id"] != user_id):
            return None
        job["cancel_requested"] = True
        if job["state"] == "queued":
            job["state"] = "cancelled"
        return job["state"]

    async def list_events_after(
        self, *, job_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        return [
            event for event in self.events if event["job_id"] == job_id and event["id"] > after_id
        ][:limit]

    def seed_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Append one stored event the way the worker surface would."""
        self.events.append(
            {
                "id": self._next_event,
                "job_id": job_id,
                "attempt_id": 1,
                "seq": self._next_event,
                "event_type": event_type,
                "payload": payload,
                "created_at": None,
            }
        )
        self._next_event += 1


class _FakeGateway:
    """The gateway's internal model catalog, answered in-process."""

    async def model_catalog(self, user_id: str) -> list[str]:
        return list(_TEST_MODELS)

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        return list(_TEST_MODELS), {}

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}

    async def user_status(self, user_id: str) -> dict[str, Any]:
        """What `require_agent_owner` re-reads before it allows a write."""
        return {"exists": True, "active": True, "role": "internal"}


class _FakeAppCredentials:
    """Just enough GitHub App to pin a ref, for the base_ref case."""

    async def resolve_ref(self, repo: str, ref: str) -> str:
        return "a" * 40


@pytest.fixture(autouse=True)
def _oncall_env(monkeypatch):
    """A configured deployment: dispatch token, service user, entitled repo.

    Individual cases unset pieces of this to pin the fail-closed answers.
    ``ADMIN_TOKEN`` is deliberately absent by default so the dedicated-token
    cases cannot pass through the admin fallback by accident.
    """
    monkeypatch.setenv("AGENT_ONCALL_DISPATCH_TOKEN", _DISPATCH_TOKEN)
    monkeypatch.setenv("AGENT_ONCALL_USER_ID", _SERVICE_USER)
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "harvardmadsys/hybridinference")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    # The admin fallback verifies sessions when a cookie arrives; none do here,
    # but the secret keeps that path deterministic if one ever does.
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    yield


@pytest.fixture()
def store() -> FakeOnCallStore:
    """Provide a fresh in-memory store."""
    return FakeOnCallStore()


def _build_app(store: FakeOnCallStore, *, app_credentials: Any | None = None) -> FastAPI:
    """Mount only the service router; auth stays real."""
    app = FastAPI()
    app.include_router(service_jobs.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_gateway_client] = _FakeGateway
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_credentials
    return app


@pytest_asyncio.fixture()
async def client(store: FakeOnCallStore):
    """HTTP client over the mounted service router."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _auth(token: str = _DISPATCH_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "repo": "HarvardMadSys/hybridInference",
        "task_prompt": "Investigate the alert below.",
        "runtime": "codex",
        "model": "glm-5.2",
        "mcp_servers": [],
    }
    body.update(overrides)
    return body


# ── The door itself ────────────────────────────────────────────────────


async def test_missing_credential_is_refused(client):
    """No bearer at all: 401, from the admin fallback's own refusal."""
    response = await client.post("/v1/agent/service/oncall/jobs", json=_create_body())
    assert response.status_code == 401


async def test_wrong_token_is_refused(client):
    """A wrong dedicated token must not fall through to anything that opens."""
    response = await client.post(
        "/v1/agent/service/oncall/jobs", json=_create_body(), headers=_auth("not-the-token")
    )
    assert response.status_code == 401


async def test_unset_dispatch_token_opens_nothing(client, monkeypatch):
    """An unset token never matches, even an empty presented one."""
    monkeypatch.delenv("AGENT_ONCALL_DISPATCH_TOKEN")
    response = await client.post(
        "/v1/agent/service/oncall/jobs", json=_create_body(), headers=_auth("")
    )
    assert response.status_code == 401


async def test_admin_token_outranks(client, monkeypatch):
    """ADMIN_TOKEN still opens the door — admin legitimately outranks dispatch."""
    monkeypatch.delenv("AGENT_ONCALL_DISPATCH_TOKEN")
    monkeypatch.setenv("ADMIN_TOKEN", _ADMIN_TOKEN)
    response = await client.post(
        "/v1/agent/service/oncall/jobs", json=_create_body(), headers=_auth(_ADMIN_TOKEN)
    )
    assert response.status_code == 201


async def test_unconfigured_service_user_is_503(client, monkeypatch):
    """No service account: the deployment cannot run on-call jobs at all."""
    monkeypatch.delenv("AGENT_ONCALL_USER_ID")
    response = await client.post(
        "/v1/agent/service/oncall/jobs", json=_create_body(), headers=_auth()
    )
    assert response.status_code == 503


# ── Creation under the service account ─────────────────────────────────


async def test_create_pins_job_to_service_account(client, store):
    """The job runs as the configured user, whatever the caller might wish."""
    response = await client.post(
        "/v1/agent/service/oncall/jobs", json=_create_body(), headers=_auth()
    )
    assert response.status_code == 201
    job = store.jobs[response.json()["id"]]
    assert job["user_id"] == _SERVICE_USER
    assert job["runtime"] == "codex"
    assert job["model"] == "glm-5.2"


async def test_oncall_mark_cannot_be_spoofed_away(client, store):
    """The platform's mark lands after caller metadata, so a body cannot lie."""
    body = _create_body(metadata={"oncall": "no-actually-not", "alert_id": "a-1"})
    response = await client.post("/v1/agent/service/oncall/jobs", json=body, headers=_auth())
    assert response.status_code == 201
    metadata = store.jobs[response.json()["id"]]["metadata"]
    assert metadata["oncall"] is True
    assert metadata["alert_id"] == "a-1"


async def test_unresolvable_model_fails_fast(client):
    """A model the service account cannot reach refuses at create, not at run."""
    response = await client.post(
        "/v1/agent/service/oncall/jobs",
        json=_create_body(model="model-that-is-not-served"),
        headers=_auth(),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_available"


async def test_unregistered_runtime_is_refused(client):
    """The registry check guards this door too, not only the owner route.

    It lives in the shared ``_create_job_as`` helper for exactly that reason:
    a check only the owner route ran would let the relay queue a job that dies
    at claim time — the drift the shared helper exists to prevent.
    """
    response = await client.post(
        "/v1/agent/service/oncall/jobs",
        json=_create_body(runtime="not-a-registered-harness"),
        headers=_auth(),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "invalid_request"


async def test_unentitled_repo_is_refused(client):
    """The allowlist is the headless account's only entitlement — no wider."""
    response = await client.post(
        "/v1/agent/service/oncall/jobs",
        json=_create_body(repo="somebody/else"),
        headers=_auth(),
    )
    assert response.status_code == 403


async def test_base_ref_is_pinned_at_create(store):
    """A branch name becomes the commit it points at now, like the owner route."""
    app = _build_app(store, app_credentials=_FakeAppCredentials())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/agent/service/oncall/jobs",
            json=_create_body(base_ref="dev"),
            headers=_auth(),
        )
    assert response.status_code == 201
    job = store.jobs[response.json()["id"]]
    assert job["base_sha"] == "a" * 40
    assert job["metadata"][BASE_REF_METADATA_KEY] == "dev"


# ── Reads and cancel, fenced to the service account ────────────────────


async def _seed_job(store: FakeOnCallStore, *, user_id: str = _SERVICE_USER) -> str:
    job = await store.create_job(
        user_id=user_id,
        repo="HarvardMadSys/hybridInference",
        task_prompt="p",
        runtime="codex",
        model="glm-5.2",
        metadata={"oncall": True},
    )
    return job["id"]


async def test_get_job_returns_own_and_hides_foreign(client, store):
    """A foreign job answers 404 — this door reads one tenant only."""
    own = await _seed_job(store)
    foreign = await _seed_job(store, user_id=_OTHER_USER)

    response = await client.get(f"/v1/agent/service/oncall/jobs/{own}", headers=_auth())
    assert response.status_code == 200
    assert response.json()["state"] == "queued"

    response = await client.get(f"/v1/agent/service/oncall/jobs/{foreign}", headers=_auth())
    assert response.status_code == 404


async def test_events_page_and_fence(client, store):
    """Events page with a cursor, and a foreign job's log stays invisible."""
    own = await _seed_job(store)
    foreign = await _seed_job(store, user_id=_OTHER_USER)
    store.seed_event(own, "tool_result", {"exit_code": 0, "content": "ok"})
    store.seed_event(own, "message", {"text": '{"summary": "done"}'})
    store.seed_event(foreign, "message", {"text": "not yours"})

    response = await client.get(f"/v1/agent/service/oncall/jobs/{own}/events", headers=_auth())
    assert response.status_code == 200
    payload = response.json()
    assert [event["event_type"] for event in payload["events"]] == ["tool_result", "message"]
    assert payload["next_cursor"] == 2

    response = await client.get(
        f"/v1/agent/service/oncall/jobs/{own}/events",
        params={"after": payload["next_cursor"]},
        headers=_auth(),
    )
    assert response.json()["events"] == []

    response = await client.get(f"/v1/agent/service/oncall/jobs/{foreign}/events", headers=_auth())
    assert response.status_code == 404


async def test_cancel_flags_job_and_fences(client, store):
    """Cancel works on the service account's job and 404s on anyone else's."""
    own = await _seed_job(store)
    foreign = await _seed_job(store, user_id=_OTHER_USER)

    response = await client.post(f"/v1/agent/service/oncall/jobs/{own}/cancel", headers=_auth())
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True
    assert store.jobs[own]["state"] == "cancelled"

    response = await client.post(f"/v1/agent/service/oncall/jobs/{foreign}/cancel", headers=_auth())
    assert response.status_code == 404
    assert store.jobs[foreign]["cancel_requested"] is False


async def test_oncall_token_cannot_create_for_a_service_user_without_agent_access(store):
    class DeniedGateway(_FakeGateway):
        async def agent_access(self, user_id):
            assert user_id == _SERVICE_USER
            return {"user_id": user_id, "allowed": False, "permissions": []}

    app = _build_app(store)
    app.dependency_overrides[get_gateway_client] = DeniedGateway
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/agent/service/oncall/jobs", json=_create_body(), headers=_auth()
        )
    assert response.status_code == 403
    assert not store.jobs


async def test_oncall_polling_and_cancel_remain_available_during_policy_outage(store):
    class UnavailableGateway(_FakeGateway):
        async def agent_access(self, user_id):
            raise AssertionError("token-authorized cleanup must not query user admission")

    job_id = await _seed_job(store)
    app = _build_app(store)
    app.dependency_overrides[get_gateway_client] = UnavailableGateway
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.get(f"/v1/agent/service/oncall/jobs/{job_id}", headers=_auth())
        ).status_code == 200
        assert (
            await client.post(f"/v1/agent/service/oncall/jobs/{job_id}/cancel", headers=_auth())
        ).status_code == 200


async def test_oncall_session_admin_uses_resolved_gateway_and_checks_service_user(store):
    from cloud_agent import identity

    asked = []

    class Gateway(_FakeGateway):
        async def agent_access(self, user_id):
            asked.append(user_id)
            return {
                "user_id": user_id,
                "allowed": True,
                "permissions": ["agent.use", "agent.admin"],
            }

    app = _build_app(store)
    app.dependency_overrides[get_gateway_client] = Gateway
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        client.cookies.set(
            identity.SESSION_COOKIE,
            identity.mint_session(user_id="operator"),
        )
        response = await client.post("/v1/agent/service/oncall/jobs", json=_create_body())
    assert response.status_code == 201
    assert asked == ["operator", _SERVICE_USER]


@pytest.fixture(autouse=True)
def _configured_identity_issuer(monkeypatch):
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
