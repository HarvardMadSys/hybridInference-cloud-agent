"""Host credential renewal and the signing key rotation on a real database (WP10, F26).

The hosts' own routes run in-process over the per-worker test database: a
host enrolls, heartbeats, receives a renewed credential, switches to it, and
the replaced one stops working after its grace. The signing key rotation then
retires the previous key only after every enrolled host holds a credential
the new key minted and nothing running holds a token from before.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from tests.integration.storage.test_schema_upgrade import _db_config, store as store

from cloud_agent import settings
from cloud_agent.api import hosts
from cloud_agent.api.deps import get_grant_client, get_renewal_schedule, get_storage
from cloud_agent.application import key_rotation, workflows
from cloud_agent.instances import CONTROL_PLANE_CAPABILITIES
from cloud_agent.storage.repositories.instances import InstanceRepository
from cloud_agent.tokens import parse_host_token
from cloud_agent_shared.deployment_contract import key_identifier

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OLD_KEY = "the-old-host-and-worker-signing-key-32b!"
NEW_KEY = "a-new-host-and-worker-signing-key-of-32b"
RENEWS = ["host-credential-renewal-v1"]


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    async def _no_sessions(self):
        return []

    monkeypatch.setattr(InstanceRepository, "unidentified_sessions", _no_sessions)
    monkeypatch.setenv("AGENT_SESSION_SECRET", "a-session-secret-of-at-least-32-bytes!!")
    hosts._fleet_renews.update(at=0.0, supported=False)

    async def _no_probe(*args, **kwargs):
        return None

    # Registration probes the sandbox manager; there is none in this test.
    monkeypatch.setattr(hosts, "_probed_sandbox_manager_error", _no_probe)
    key_rotation.register_advancers()
    yield
    settings.reset_settings()


def _app(storage: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(hosts.router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_grant_client] = lambda: None
    app.dependency_overrides[get_renewal_schedule] = lambda: None
    return app


async def _sql(statement: str, *args: Any) -> Any:
    conn = await asyncpg.connect(**_db_config())
    try:
        return await conn.fetch(statement, *args)
    finally:
        await conn.close()


async def _adopt(store) -> int:
    revision, encoded = await store.settings.read_settings()
    settings.apply_settings(revision, encoded)
    await _sql("DELETE FROM agent_control_plane_instances")
    await store.instances.report(
        instance_id=uuid.uuid4().hex,
        role="api",
        machine="cp-1",
        build_sha="",
        capabilities=list(CONTROL_PLANE_CAPABILITIES),
        adopted_settings_revision=revision,
        restart_required=[],
        details={},
        started_at=datetime.now(UTC),
    )
    return revision


async def _seed(store) -> None:
    await _sql("DELETE FROM agent_deploy_workflows")
    settings.reset_settings()
    await settings.initialize_settings(store.settings)
    revision, _ = await store.settings.read_settings()
    await store.settings.update_settings(
        settings.encode_changes({"control_signing_secret": OLD_KEY}), expected_revision=revision
    )
    await _adopt(store)


async def _enroll(client: httpx.AsyncClient, store, name: str) -> str:
    token = f"enroll-{name}-{uuid.uuid4().hex}"
    await store.host_pool.issue_enroll_token(
        token_hash=hashlib.sha256(token.encode()).hexdigest(), host=name, ttl_seconds=600
    )
    answer = await client.post(
        "/v1/agent/host/register",
        json={
            "enroll_token": token,
            "name": name,
            "slots_total": 1,
            "capabilities": RENEWS,
            "sandbox_manager_url": "http://sandbox-manager.test:8092",
        },
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["credential"]


async def _beat(client: httpx.AsyncClient, credential: str, capabilities=RENEWS) -> httpx.Response:
    return await client.post(
        "/v1/agent/host/heartbeat",
        json={"slots_total": 1, "capabilities": capabilities},
        headers={"Authorization": f"Bearer {credential}"},
    )


async def test_a_host_switches_to_a_renewed_credential_and_the_old_one_lapses(store) -> None:
    await _seed(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)), base_url="http://cp"
    ) as client:
        first = await _enroll(client, store, "gpu-1")
        beat = await _beat(client, first)
        assert beat.status_code == 200, beat.text
        # Its credential was minted by the key that signs now: nothing to renew.
        assert beat.json()["credential_renewal"] is None

        assert await store.host_credentials.request_renewal("gpu-1")
        renewed = (await _beat(client, first)).json()["credential_renewal"]
        assert renewed and renewed != first
        # The answer may be lost: the same renewal comes again, not a second one.
        assert (await _beat(client, first)).json()["credential_renewal"] == renewed
        # A host that cannot keep a renewal is never sent one.
        assert (await _beat(client, first, capabilities=[])).json()["credential_renewal"] is None

        switched = await _beat(client, renewed)
        assert switched.status_code == 200
        assert switched.json()["credential_renewal"] is None
        # The replaced credential still works within its grace (a second runner
        # on the machine reads the file at its next beat) ...
        assert (await _beat(client, first)).status_code == 200
        await _sql(
            "UPDATE agent_runner_hosts SET previous_credential_expires_at = NOW() - INTERVAL '1 second'"
        )
        # ... and not after it.
        assert (await _beat(client, first)).status_code == 401
        assert (await _beat(client, renewed)).status_code == 200

        # Re-enrolling the name revokes every earlier credential at once.
        await _sql(
            "UPDATE agent_runner_hosts SET credential_renewal_requested = TRUE WHERE host = 'gpu-1'"
        )
        pending = (await _beat(client, renewed)).json()["credential_renewal"]
        again = await _enroll(client, store, "gpu-1")
        assert (await _beat(client, renewed)).status_code == 401
        assert (await _beat(client, pending)).status_code == 401
        assert (await _beat(client, again)).status_code == 200


async def test_a_signing_key_rotation_retires_only_after_every_host_renewed_and_tokens_drained(
    store,
) -> None:
    await _seed(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)), base_url="http://cp"
    ) as client:
        live = await _enroll(client, store, "gpu-1")
        await _beat(client, live)
        await _enroll(client, store, "gpu-2")  # enrolled, never seen since
        workflow = await key_rotation.start_rotation(
            store,
            kind="control_signing",
            params={},
            sealed_by_agent={},
            secret=NEW_KEY,
            admin_id="admin-token",
        )
        workflow_id = workflow["workflow_id"]

        async def advance() -> dict[str, Any]:
            await _adopt(store)
            current = await store.deployments.get_workflow(workflow_id)
            await workflows.advance_workflow(store, current)
            return await store.deployments.get_workflow(workflow_id)

        state = await advance()
        assert state["step"] == "renew_hosts" and state["state"] == "waiting", state
        assert "gpu-1 has not received a renewal yet" in state["detail"]
        assert "gpu-2 is enrolling" in state["detail"]
        # A token the previous key signed still verifies during the window.
        assert parse_host_token(live)["host"] == "gpu-1"

        renewed = (await _beat(client, live)).json()["credential_renewal"]
        assert renewed
        assert (await _beat(client, renewed)).status_code == 200
        state = await advance()
        assert "gpu-1" not in state["detail"] and "gpu-2 is enrolling" in state["detail"]
        # The administrator removes the server that never came back.
        await _sql("UPDATE agent_runner_hosts SET status = 'disabled' WHERE host = 'gpu-2'")

        # A turn that started before the new key signed holds an old token.
        conn = await asyncpg.connect(**_db_config())
        try:
            await conn.execute("SET session_replication_role = replica")
            await conn.execute(
                "INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner, "
                "lease_generation, lease_expires_at, started_at) "
                "VALUES ('j-old', 't-old', 1, 'gpu-1', 1, NOW() + INTERVAL '1 hour', "
                "NOW() - INTERVAL '1 day')"
            )
        finally:
            await conn.close()
        state = await advance()
        assert state["step"] == "drain_tokens" and "1 running turns" in state["detail"], state
        await _sql(
            "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
            "WHERE job_id = 'j-old'"
        )

        state = await advance()
        state = await advance()
        assert state["state"] == "succeeded", state
        _revision, encoded = await store.settings.read_settings()
        assert settings._decode(encoded)["control_signing_secret_previous"] == ""
        rows = await _sql(
            "SELECT credential_key_id, previous_credential_hash FROM agent_runner_hosts "
            "WHERE host = 'gpu-1'"
        )
        assert rows[0]["credential_key_id"] == key_identifier(NEW_KEY)
        assert rows[0]["previous_credential_hash"] is None
        # The renewed credential keeps working; the pre-rotation one no longer verifies.
        assert (await _beat(client, renewed)).status_code == 200
        assert (await _beat(client, live)).status_code == 401
