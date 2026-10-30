"""Deployment agents end to end on a real database: enroll, intents, results, rollout (WP6-WP8).

The control plane's routes run in-process over the test database; the agent
side is the real agent code on a temporary deployment root with a recorded
Docker. What crosses between them is exactly what crosses the network.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
import pytest
from deploy.agent import releases
from deploy.agent.cli import main
from deploy.agent.database import register as register_database
from deploy.agent.operations import Executor
from deploy.agent.rotation import register as register_rotation
from deploy.agent.store import AgentStore, parse_env, write_json
from fastapi import FastAPI
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.deploy_agent_fakes import FakeMachine
from tests.unit.deploy.test_deploy_agent import _release

from cloud_agent.api import admin_deployments, deploy_agents
from cloud_agent.api.deps import get_storage, verify_admin_access
from cloud_agent_shared.deployment_contract import seal, sealed_purpose

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _no_audit(*args: Any, **kwargs: Any) -> None:
    return None


def _app(storage: Any, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_deployments.router)
    app.include_router(deploy_agents.router)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[verify_admin_access] = lambda: "admin:operator"
    monkeypatch.setattr(admin_deployments, "log_admin_action", _no_audit)
    return app


def _machine(tmp_path: Path, role: str) -> tuple[AgentStore, Executor, FakeMachine]:
    root = tmp_path / role
    root.mkdir(parents=True)
    (root / ".env").write_text("AGENT_RUNNER_HOST=gpu-1\n" if role == "host" else "")
    assert main(["--root", str(root), "init", "--role", role, "--name", f"{role}-1"]) == 0
    agent_store = AgentStore(root)
    write_json(agent_store.root / "policy.json", {"compose": {"project": "p"}}, 0o644)
    machine = FakeMachine(root)
    executor = Executor(agent_store, machine, health_timeout=0.05, health_interval=0.01)
    executor.http_check = lambda url: True
    register_database(executor)
    register_rotation(executor)
    return agent_store, executor, machine


async def _enroll(client: httpx.AsyncClient, agent_store: AgentStore, role: str) -> str:
    token = (
        await client.post("/admin/agent/deployments/enroll-token", json={"role": role})
    ).json()["token"]
    identity = agent_store.identity()
    answer = await client.post(
        "/v1/agent/deploy/register",
        json={
            "token": token,
            "name": identity["name"],
            "role": role,
            "machine": identity["machine"],
            "public_key": identity["public_key"],
        },
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    identity["agent_id"] = body["agent_id"]
    agent_store.save_identity(identity)
    agent_store.save_credential(body["credential"])
    # The token is spent.
    again = await client.post(
        "/v1/agent/deploy/register", json={**json.loads(answer.request.content), "name": "other"}
    )
    assert again.status_code == 403
    return body["credential"]


async def _sync(client: httpx.AsyncClient, executor: Executor, credential: str) -> list[str]:
    headers = {"Authorization": f"Bearer {credential}"}
    store = executor.store
    report = await client.post(
        "/v1/agent/deploy/report",
        json={"observed": executor.observed(), "operations": []},
        headers=headers,
    )
    assert report.status_code == 200, report.text
    fetched = (
        await client.get(
            f"/v1/agent/deploy/intents?after_seq={store.state().get('last_seq', 0)}",
            headers=headers,
        )
    ).json()["intents"]
    states = []
    for intent in fetched:
        outcome = executor.execute(
            intent["operation"],
            intent["params"],
            intent["sealed"],
            intent_id=intent["intent_id"],
            seq=intent["seq"],
            origin="intent",
        )
        posted = await client.post(
            f"/v1/agent/deploy/intents/{intent['intent_id']}/result",
            json={"state": outcome.state, "detail": outcome.detail, "evidence": outcome.evidence},
            headers=headers,
        )
        assert posted.json()["accepted"] is True
        states.append(outcome.state)
    await client.post(
        "/v1/agent/deploy/report",
        json={"observed": executor.observed(), "operations": []},
        headers=headers,
    )
    return states


async def test_an_intent_round_trip_with_a_sealed_value(store, tmp_path, monkeypatch) -> None:
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        agent_store, executor, _ = _machine(tmp_path, "control-plane")
        credential = await _enroll(client, agent_store, "control-plane")
        agent_id = agent_store.identity()["agent_id"]
        await _sync(client, executor, credential)
        public = (await client.get("/admin/agent/deployments")).json()["agents"][0]["public_key"]
        box = seal(
            public, "a-new-admin-token-thirty-two-bytes!!", sealed_purpose(agent_id, "ADMIN_TOKEN")
        )
        request = {
            "operation": "apply_config",
            "params": {"changes": {"AGENT_WEB_PORT": "3010"}},
            "sealed": {"ADMIN_TOKEN": box},
            "idempotency_key": "change-web-port-1",
        }
        created = await client.post(
            f"/admin/agent/deployments/agents/{agent_id}/intents", json=request
        )
        assert created.status_code == 200, created.text
        # The same request again is the same intent.
        same = await client.post(
            f"/admin/agent/deployments/agents/{agent_id}/intents", json=request
        )
        assert same.json()["intent_id"] == created.json()["intent_id"]
        assert await _sync(client, executor, credential) == ["succeeded"]
        env = parse_env(agent_store.current_env())
        assert env["AGENT_WEB_PORT"] == "3010"
        assert env["ADMIN_TOKEN"] == "a-new-admin-token-thirty-two-bytes!!"
        overview = (await client.get("/admin/agent/deployments")).json()
        assert overview["intents"][0]["state"] == "succeeded"
        assert "a-new-admin-token" not in json.dumps(overview)
    # The control plane kept no ciphertext once the intent finished.
    conn = await asyncpg.connect(**_db_config())
    try:
        sealed = await conn.fetchval("SELECT sealed::text FROM agent_deploy_intents")
    finally:
        await conn.close()
    assert sealed == "{}"


async def test_contract_and_authority_refusals(store, tmp_path, monkeypatch) -> None:
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        agent_store, executor, _ = _machine(tmp_path, "host")
        credential = await _enroll(client, agent_store, "host")
        agent_id = agent_store.identity()["agent_id"]
        await _sync(client, executor, credential)
        base = f"/admin/agent/deployments/agents/{agent_id}/intents"
        bad = await client.post(
            base, json={"operation": "shell", "params": {}, "idempotency_key": "k-000001"}
        )
        assert bad.status_code == 422
        unconfirmed = await client.post(
            base,
            json={
                "operation": "rollback_config",
                "params": {"to_rev": 0},
                "idempotency_key": "k-000002",
            },
        )
        assert unconfirmed.status_code == 422 and "confirm" in unconfirmed.json()["detail"]
        not_drained = await client.post(
            base,
            json={
                "operation": "kata_install",
                "params": {"version": "3.32.0", "sha256": "1" * 64},
                "idempotency_key": "k-000003",
                "confirm": "kata_install",
            },
        )
        assert not_drained.status_code == 422
        # An agent credential is not an administrator, and an admin credential is not an agent.
        assert (
            await client.get(
                "/v1/agent/deploy/intents", headers={"Authorization": "Bearer admin-x"}
            )
        ).status_code == 401
        revoked = await client.post(
            f"/admin/agent/deployments/agents/{agent_id}/revoke", json={"confirm": "host-1"}
        )
        assert revoked.status_code == 200
        gone = await client.get(
            "/v1/agent/deploy/intents", headers={"Authorization": f"Bearer {credential}"}
        )
        assert gone.status_code == 401


async def test_a_rollout_activates_hosts_before_the_control_plane(
    store, tmp_path, monkeypatch
) -> None:
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        cp_store, cp, cp_machine = _machine(tmp_path, "control-plane")
        host_store, host, host_machine = _machine(tmp_path, "host")
        cp_credential = await _enroll(client, cp_store, "control-plane")
        host_credential = await _enroll(client, host_store, "host")
        (tmp_path / "rel").mkdir()
        manifest, recipe, digest = _release(tmp_path / "rel", "f" * 40)
        for agent_store in (cp_store, host_store):
            releases.import_release(agent_store, manifest, recipe, digest)
        cp_machine.activate_writes = host_machine.activate_writes = "f" * 40
        await _sync(client, cp, cp_credential)
        await _sync(client, host, host_credential)
        started = await client.post(
            "/admin/agent/deployments/rollouts",
            json={"manifest_sha256": digest, "confirm": "release_activate"},
        )
        assert started.status_code == 200, started.text
        workflow_id = started.json()["workflow_id"]
        # prepare on both
        assert await _sync(client, cp, cp_credential) == ["succeeded"]
        assert await _sync(client, host, host_credential) == ["succeeded"]
        # prepare-database on the control plane; the host has nothing yet
        assert await _sync(client, host, host_credential) == []
        assert await _sync(client, cp, cp_credential) == ["succeeded"]
        # the control plane is not activated before the host
        assert await _sync(client, cp, cp_credential) == []
        assert await _sync(client, host, host_credential) == ["succeeded"]
        assert await _sync(client, cp, cp_credential) == ["succeeded"]
        workflow = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert workflow["state"] == "succeeded", workflow
        phases = [
            (intent["agent_id"] == cp_store.identity()["agent_id"], intent["operation"])
            for intent in sorted(workflow["intents"], key=lambda i: i["created_at"])
        ]
        assert phases.index((False, "release_activate")) < phases.index((True, "release_activate"))
        assert (
            await client.post(
                "/admin/agent/deployments/rollouts",
                json={"manifest_sha256": "0" * 64, "confirm": "release_activate"},
            )
        ).status_code == 409


async def test_a_failed_host_activation_stops_the_rollout_before_the_control_plane(
    store, tmp_path, monkeypatch
) -> None:
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        cp_store, cp, cp_machine = _machine(tmp_path, "control-plane")
        host_store, host, host_machine = _machine(tmp_path, "host")
        cp_credential = await _enroll(client, cp_store, "control-plane")
        host_credential = await _enroll(client, host_store, "host")
        (tmp_path / "rel").mkdir()
        manifest, recipe, digest = _release(tmp_path / "rel", "f" * 40)
        for agent_store in (cp_store, host_store):
            releases.import_release(agent_store, manifest, recipe, digest)
        cp_machine.activate_writes = "f" * 40
        host_machine.activate_writes = None  # the host never records the release
        await _sync(client, cp, cp_credential)
        await _sync(client, host, host_credential)
        workflow_id = (
            await client.post(
                "/admin/agent/deployments/rollouts",
                json={"manifest_sha256": digest, "confirm": "release_activate"},
            )
        ).json()["workflow_id"]
        await _sync(client, cp, cp_credential)
        await _sync(client, host, host_credential)
        await _sync(client, cp, cp_credential)
        assert await _sync(client, host, host_credential) == ["failed"]
        assert await _sync(client, cp, cp_credential) == []
        workflow = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert workflow["state"] == "failed"
        assert "not switched" in workflow["detail"]
