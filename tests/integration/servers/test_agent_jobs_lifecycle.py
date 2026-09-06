"""End-to-end agent job lifecycle over the real Postgres-backed store.

The API-surface tests use an in-memory stand-in; this one wires the router to
a real ``AgentJobStore`` so the HTTP layer and the fencing SQL are exercised
together — including the zombie-worker case, where a reaped attempt's token
still parses but every write must be rejected with 409.

Ported from the gateway with assertions untouched. The schema comes from
``alembic upgrade head`` rather than ``store.initialize()``, which is a no-op
here; the model catalog is a fake gateway; and the owner identity is stubbed at
``authenticate_agent_owner`` — see the fixture for why that is not the same
override the source wrote.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import routes as agent_jobs_router
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_agent_job_store,
    get_gateway_client,
    get_grant_client,
    get_log_store,
)
from cloud_agent.storage.store import AgentJobStore
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_TABLES_IN_FK_ORDER = (
    "agent_native_session_edges",
    "agent_thread_sandbox_memberships",
    "agent_sandbox_snapshots",
    "agent_session_forks",
    "agent_sandboxes",
    "agent_thread_messages",
    "agent_job_artifacts",
    "agent_job_events",
    "agent_thread_sandboxes",
    "agent_thread_residencies",
    "agent_attempts",
    "agent_jobs",
    "agent_threads",
    # No FK, but every claim writes here: a host left active by one test would
    # gate the claims of the next one.
    "agent_runner_hosts",
)
_OWNER = "user-owner"
_TEST_MODEL = "glm-5.1"


def _claim_body(worker_id: str) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "attestation": {
            "sandbox_image_digest": "sha256:lifecycle-test-image",
            "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
            "runner_capabilities": [
                SHARED_SANDBOX_CAPABILITY,
                NATIVE_FORK_AT_ANCHOR_CAPABILITY,
                shared_deployment_capability("test-deployment"),
            ],
            "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
            "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
        },
    }


class _FakeGateway:
    """The model catalog, which the create endpoint fails fast against."""

    async def model_catalog(self, user_id: str) -> list[str]:
        return [_TEST_MODEL]

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        # The interface the create path actually calls now; no aliases, which
        # is exactly what an older gateway answers.
        return [_TEST_MODEL], {}

    async def user_status(self, user_id: str) -> dict[str, Any]:
        # Read by `require_agent_owner` on every write. A fake missing it fails
        # the whole mutating surface with an AttributeError rather than a
        # refusal — which is how it was found.
        return {"exists": True, "active": True, "role": "admin"}

    # No `mcp_registry` here any more. A fake that answers a question the real
    # client cannot ask is a test passing against an interface nothing has —
    # and it would have kept this suite green through the whole amendment.


class _FakeGrants:
    """The gateway's grant contract. The claim asks for one now (E9)."""

    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "grant_id": f"agr_{kwargs['attempt_id']}",
            "token": "agr.token",
            "allowed_models": list(kwargs.get("models") or []),
        }

    async def renew(self, grant_id: str) -> dict[str, Any]:
        return {"grant_id": grant_id}

    async def revoke(self, grant_id: str) -> bool:
        return True


@pytest_asyncio.fixture()
async def api(monkeypatch):
    """Mount the router over a real AgentJobStore with auth stubbed."""
    import asyncpg

    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "lifecycle-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "lifecycle-broker-secret")
    # The allowlist is empty unless a deployment sets it, and an unentitled
    # repo is refused at creation, so declare the one repo these tests use.
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    store = AgentJobStore(pool)
    # ``initialize()`` is a no-op now — the schema and its backfills are the
    # baseline migration's, and a deploy applies them the same way.
    apply_migrations(db_config)
    async with pool.acquire() as conn:
        for table in _TABLES_IN_FK_ORDER:
            await conn.execute(f"DELETE FROM {table}")
        await conn.execute("UPDATE agent_runner_policy SET active_host = NULL")
    await store.register_host(
        host="runner-test",
        credential_hash="test-credential-hash",
        capabilities=[SHARED_SANDBOX_CAPABILITY],
        slots_total=4,
    )
    await store.record_host_heartbeat(
        host="runner-test",
        capabilities=[SHARED_SANDBOX_CAPABILITY],
    )

    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    app.dependency_overrides[get_gateway_client] = _FakeGateway
    app.dependency_overrides[get_grant_client] = _FakeGrants
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    # No log store here either: the source's app had no services container,
    # so spend and usage were absent rather than a fabricated zero.
    app.dependency_overrides[get_log_store] = lambda: None
    # The source overrode ``verify_api_key`` with ``role: "pro"``, which never
    # took effect: ``authenticate_agent_owner`` calls that dependency directly,
    # so FastAPI could not intercept it, and the real one answered with an
    # anonymous *admin* context because user auth was disabled in that test
    # environment. Overriding the credential resolver is the override the
    # source meant; ``admin`` is the least privilege the router's own gate
    # accepts, so the lifecycle stays the subject of the test.
    # (``verify_api_key`` no longer exists here — the API-key branch was deleted
    # on 2026-08-04 — so this paragraph is about the source, not about a
    # dependency a reader will find.)
    app.dependency_overrides[agent_jobs_router.authenticate_agent_owner] = lambda: {
        "user_id": _OWNER,
        "role": "admin",
        "authenticated": True,
    }
    # /worker/claim is machine-to-machine; stand in for an enrolled host credential.
    app.dependency_overrides[agent_jobs_router.verify_claiming_host] = lambda: {
        "host": "runner-test",
        "credential_hash": "test-credential-hash",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, pool

    async with pool.acquire() as conn:
        for table in _TABLES_IN_FK_ORDER:
            await conn.execute(f"DELETE FROM {table}")
        await conn.execute("UPDATE agent_runner_policy SET active_host = NULL")
    await pool.close()


async def test_reaped_worker_token_is_rejected_end_to_end(api):
    """A zombie worker's token parses but every write is fenced out with 409."""
    client, store, pool = api

    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "slow task", "model": "glm-5.1"},
    )
    job_id = created.json()["id"]
    claim = await client.post("/v1/agent/worker/claim", json=_claim_body("w1"))
    zombie_auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    attempt_id = claim.json()["attempt_id"]

    # The lease expires and the reaper requeues the job as a new attempt.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            attempt_id,
        )
    actions = await store.reap_expired(max_attempts=3)
    assert actions[0]["action"] == "queued"

    # Every remaining worker write path of the zombie is now rejected.
    for path, payload in (
        (f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
        (f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        (f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
    ):
        response = await client.post(path, json=payload, headers=zombie_auth)
        assert response.status_code == 409, path

    # A fresh worker takes over and can write.
    retry = await client.post("/v1/agent/worker/claim", json=_claim_body("w2"))
    assert retry.json()["attempt_no"] == 2
    live_auth = {"Authorization": f"Bearer {retry.json()['worker_token']}"}
    assert (
        await client.post(
            f"/v1/agent/worker/jobs/{job_id}/events",
            json={"event_type": "message", "payload": {"text": "retry"}},
            headers=live_auth,
        )
    ).status_code == 201

    # The supersede is visible to the owner as an append-only control event.
    events = (await client.get(f"/v1/agent/jobs/{job_id}/events")).json()["events"]
    assert [event["event_type"] for event in events] == ["attempt_superseded", "message"]


async def test_cancel_running_job_is_delivered_via_heartbeat(api):
    """Owner cancel surfaces to the worker, which performs the transition."""
    client, _store, _pool = api

    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "long task", "model": "glm-5.1"},
    )
    job_id = created.json()["id"]
    claim = await client.post("/v1/agent/worker/claim", json=_claim_body("w1"))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    cancel = await client.post(f"/v1/agent/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "running"
    assert cancel.json()["cancel_requested"] is True

    beat = await client.post(f"/v1/agent/worker/jobs/{job_id}/heartbeat", json={}, headers=auth)
    assert beat.json()["cancel_requested"] is True

    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "cancelled"}, headers=auth
    )
    assert finish.status_code == 200
    assert (await client.get(f"/v1/agent/jobs/{job_id}")).json()["state"] == "cancelled"
