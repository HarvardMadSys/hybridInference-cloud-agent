"""Credential and tunnel key rotations across managed and self-managed servers.

The workflows, settings, hosts and agents are real and run over the test
database. What stands in for the outside world is the SSH executor's remote end
(a recording ``bootstrap`` and ``tunnels``; their remote scripts have their
own tests against temporary roots) and the sandbox manager's health route,
which answers from what each fake server was told.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from deploy.agent.store import write_json
from tests.integration.storage.test_deployment_agents import _app, _enroll, _machine, _sync
from tests.integration.storage.test_schema_upgrade import _db_config, store as store

from cloud_agent import settings
from cloud_agent.application import (
    key_rotation,
    managed_hosts,
    sandbox_token_rotation,
    tunnel_key_rotation,
    workflows,
)
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.instances import CONTROL_PLANE_CAPABILITIES
from cloud_agent.providers.compute.setup_tunnels import generate_tunnel_key
from cloud_agent_shared.deployment_contract import key_identifier, seal, sealed_purpose

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OLD = "the-old-sandbox-manager-credential-32b!"
NEW = "a-new-sandbox-manager-credential-of-32b"
COMPUTE_KEY = "a-compute-key-of-at-least-thirty-two-b!!"


class FakeRepository:
    """Compute inventory: one allocation per server, each with its stored profile."""

    def __init__(self) -> None:
        self.allocations: list[dict[str, Any]] = []
        self.rows: dict[str, dict[str, Any]] = {}
        self.secrets = ComputeSecretStore(self, key=COMPUTE_KEY)  # type: ignore[arg-type]
        self.keys: dict[str, dict[str, Any]] = {}

    def add(self, host: str, *, automatic: bool = True, stored: bool = True) -> None:
        profile_id = f"auto-{host}"
        keys = {role: generate_tunnel_key() for role in ("gateway", "mcp", "control")}
        self.keys[host] = keys
        node_id = str(uuid.uuid4())
        self.allocations.append(
            {
                "id": f"alloc-{host}",
                "observed_state": "running",
                "deployment_profile": {
                    "id": profile_id,
                    "label": host,
                    "source_bundle": f"stored-profile:{profile_id}:bundle",
                    "source_sha256": "0" * 64,
                    "deployment_sha": "0" * 40,
                    "environment_ref": (
                        f"stored-profile:{profile_id}:environment" if stored else "deployment:env"
                    ),
                },
                "nodes": [
                    {
                        "id": node_id,
                        "name": host,
                        "provider_node_id": host,
                        "instance_identity": host,
                        "connection": None,
                    }
                ],
                "hosts": [{"host": host, "status": "online", "compute_node_id": node_id}],
            }
        )
        inputs = {
            "environment": f"AGENT_SANDBOX_MANAGER_TOKEN='{OLD}'\nOTHER='kept'\n",
            "files": {
                f".compute-secrets/{r}.key": k.private_key.get_secret_value()
                for r, k in keys.items()
            },
            "public_keys": {r: k.public_key for r, k in keys.items()},
        }
        self.rows[profile_id] = {
            "id": profile_id,
            "configuration": {
                "automatic_setup": {
                    "ready": True,
                    "reverse_port": 21000,
                    "endpoint": {
                        "main_port": 8100,
                        "mcp_port": 8101,
                        "gateway_ip": "172.20.0.1",
                        "machine_identity": "b" * 64,
                    },
                }
                if automatic
                else {}
            },
            "secret_ciphertext": self.secrets.encrypt(
                "profile:" + profile_id, "deployment_profile", json.dumps(inputs)
            ),
        }

    async def list_allocations(self) -> list[dict[str, Any]]:
        return self.allocations

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        return self.rows.get(profile_id)

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        return None

    async def provider_defaults(self, provider_id: str) -> dict[str, Any]:
        machine = {"connection_id": "c", "address": "10.0.0.1", "username": "u"}
        return {
            "request": {
                "request_id": "r",
                "gateway": machine,
                "control": machine,
                "chat_network_pool": "172.29.0.0/16",
                "dns_server": "1.1.1.1",
                "protected_cidrs": ["192.0.2.0/24"],
            }
        }

    async def replace_profile_secret(
        self, *, profile_id: str, expected_ciphertext: str, secret_ciphertext: str
    ) -> bool:
        row = self.rows[profile_id]
        if row["secret_ciphertext"] != expected_ciphertext:
            return False
        row["secret_ciphertext"] = secret_ciphertext
        return True

    def inputs(self, host: str) -> dict[str, Any]:
        profile_id = f"auto-{host}"
        return json.loads(
            self.secrets.decrypt(
                "profile:" + profile_id,
                "deployment_profile",
                self.rows[profile_id]["secret_ciphertext"],
            )
        )


class FakeBootstrap:
    """Remembers what each server holds and in what order servers were reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.accepts: dict[str, set[str]] = {}
        self.fail_on: str | None = None

    async def apply_environment(self, node, profile, intent, **change) -> None:
        from cloud_agent.ports.external.compute_provider import ProviderError

        if intent.host == self.fail_on:
            raise ProviderError("services_unhealthy", "The Host did not stay up")
        self.calls.append((intent.host, change))
        accepted = self.accepts.setdefault(intent.host, {OLD})
        changes = change.get("changes") or {}
        if "AGENT_SANDBOX_MANAGER_TOKEN_NEXT" in changes:
            accepted.add(changes["AGENT_SANDBOX_MANAGER_TOKEN_NEXT"])
        if "AGENT_SANDBOX_MANAGER_TOKEN" in changes:
            accepted.intersection_update({changes["AGENT_SANDBOX_MANAGER_TOKEN"]})


class FakeTunnels:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def rotate_gateway(self, connection, **kwargs) -> None:
        self.calls.append(("gateway", kwargs["mode"]))

    async def rotate_control(self, connection, **kwargs) -> None:
        self.calls.append(("control", kwargs["mode"]))


class FakeSetup:
    async def _connection(self, machine) -> str:
        return "connection"


@pytest.fixture()
def fleet(monkeypatch):
    repository = FakeRepository()
    bootstrap, tunnels = FakeBootstrap(), FakeTunnels()
    executor = managed_hosts.ManagedHosts(
        repository=repository,
        credential_rotation=repository,
        bootstrap=bootstrap,
        secrets=repository.secrets,
        tunnels=tunnels,
        setup=FakeSetup(),
    )
    managed_hosts.configure(executor)

    async def reachable(storage: Any, host: str, token: str) -> bool:
        return token in bootstrap.accepts.get(host, {OLD})

    monkeypatch.setattr(sandbox_token_rotation, "_reachable_with", reachable)
    monkeypatch.setenv("AGENT_SESSION_SECRET", "a-session-secret-of-at-least-32-bytes!!")
    key_rotation.register_advancers()
    yield repository, bootstrap, tunnels
    managed_hosts.configure(None)
    settings.reset_settings()


async def _sql(statement: str, *args: Any) -> Any:
    conn = await asyncpg.connect(**_db_config())
    try:
        return await conn.fetch(statement, *args)
    finally:
        await conn.close()


async def _online(store, *hosts: str) -> None:
    await _sql("DELETE FROM agent_deploy_workflows")
    for host in hosts:
        await store.host_pool.register_host(
            host=host,
            credential_hash=f"hash-{host}",
            capabilities=[],
            slots_total=1,
            sandbox_manager_url="http://sandbox-manager.test:8092",
            runner_version="x",
            labels={},
            enrollment_token_hash=None,
        )
        await _sql(
            "UPDATE agent_runner_hosts SET status = 'online', last_heartbeat_at = NOW() "
            "WHERE host = $1",
            host,
        )


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


async def _settings(store) -> None:
    settings.reset_settings()
    await settings.initialize_settings(store.settings)
    revision, _ = await store.settings.read_settings()
    await store.settings.update_settings(
        settings.encode_changes({"sandbox_manager_token": OLD}), expected_revision=revision
    )
    await _adopt(store)


async def _advance(store, workflow_id: str, rounds: int = 30) -> dict[str, Any]:
    for _ in range(rounds):
        await managed_hosts.settle()
        await _adopt(store)
        workflow = await store.deployments.get_workflow(workflow_id)
        if workflow["state"] not in {"running", "waiting"}:
            return workflow
        await workflows.advance_workflow(store, workflow)
        await managed_hosts.settle()
    return await store.deployments.get_workflow(workflow_id)


async def test_the_sandbox_manager_credential_rotates_on_managed_servers_one_at_a_time(
    store, fleet
) -> None:
    repository, bootstrap, _tunnels = fleet
    await _online(store, "gpu-1", "gpu-2")
    repository.add("gpu-1")
    repository.add("gpu-2")
    await _settings(store)
    workflow = await key_rotation.start_rotation(
        store,
        kind="sandbox_manager_token",
        params={},
        sealed_by_agent={},
        secret=NEW,
        admin_id="admin-token",
    )
    final = await _advance(store, workflow["workflow_id"])
    assert final["state"] == "succeeded", final["detail"]
    assert [(host, list(c.get("changes", {}))) for host, c in bootstrap.calls] == [
        ("gpu-1", ["AGENT_SANDBOX_MANAGER_TOKEN_NEXT"]),
        ("gpu-2", ["AGENT_SANDBOX_MANAGER_TOKEN_NEXT"]),
        ("gpu-1", ["AGENT_SANDBOX_MANAGER_TOKEN"]),
        ("gpu-2", ["AGENT_SANDBOX_MANAGER_TOKEN"]),
    ]
    assert all(c["services"] == ("sandbox-manager", "runner") for _h, c in bootstrap.calls)
    assert bootstrap.calls[2][1]["remove"] == ("AGENT_SANDBOX_MANAGER_TOKEN_NEXT",)
    _revision, encoded = await store.settings.read_settings()
    assert settings._decode(encoded)["sandbox_manager_token"] == NEW
    # A later redeploy or node replacement renders the new credential.
    for host in ("gpu-1", "gpu-2"):
        assert f"AGENT_SANDBOX_MANAGER_TOKEN='{NEW}'" in repository.inputs(host)["environment"]
        assert "OTHER='kept'" in repository.inputs(host)["environment"]
    stored = json.dumps(await store.deployments.get_workflow(workflow["workflow_id"]), default=str)
    assert NEW not in stored and OLD not in stored
    assert "next_token" not in stored


async def test_the_new_credential_is_not_used_until_every_server_proved_it_accepts_it(
    store, fleet, monkeypatch
) -> None:
    repository, _bootstrap, _tunnels = fleet
    await _online(store, "gpu-1")
    repository.add("gpu-1")
    await _settings(store)

    async def refuses(storage: Any, host: str, token: str) -> bool:
        return False

    monkeypatch.setattr(sandbox_token_rotation, "_reachable_with", refuses)
    workflow = await key_rotation.start_rotation(
        store,
        kind="sandbox_manager_token",
        params={},
        sealed_by_agent={},
        secret=NEW,
        admin_id="admin-token",
    )
    state = await _advance(store, workflow["workflow_id"], rounds=4)
    assert state["step"] == "verify_next" and state["state"] == "waiting", state
    assert "gpu-1" in state["detail"]
    _revision, encoded = await store.settings.read_settings()
    assert settings._decode(encoded)["sandbox_manager_token"] == OLD


async def test_a_server_no_executor_reaches_refuses_the_start_by_name(store, fleet) -> None:
    repository, _bootstrap, _tunnels = fleet
    await _online(store, "gpu-1", "elsewhere", "filed")
    repository.add("gpu-1")
    repository.add("filed", stored=False)
    await _settings(store)
    with pytest.raises(workflows.WorkflowRefused) as refused:
        await key_rotation.start_rotation(
            store,
            kind="sandbox_manager_token",
            params={},
            sealed_by_agent={},
            secret=NEW,
            admin_id="admin-token",
        )
    assert "elsewhere (no deployment agent)" in str(refused.value)
    assert "filed (its deployment inputs are files, not stored)" in str(refused.value)


async def test_a_managed_server_that_fails_stops_the_rotation_on_the_old_credential(
    store, fleet
) -> None:
    repository, bootstrap, _tunnels = fleet
    await _online(store, "gpu-1", "gpu-2")
    repository.add("gpu-1")
    repository.add("gpu-2")
    await _settings(store)
    bootstrap.fail_on = "gpu-2"
    workflow = await key_rotation.start_rotation(
        store,
        kind="sandbox_manager_token",
        params={},
        sealed_by_agent={},
        secret=NEW,
        admin_id="admin-token",
    )
    final = await _advance(store, workflow["workflow_id"])
    assert final["state"] == "failed" and "gpu-2" in final["detail"]
    assert "did not stay up" in final["detail"]
    _revision, encoded = await store.settings.read_settings()
    assert settings._decode(encoded)["sandbox_manager_token"] == OLD

    # The server recovers: the rotation continues from there, not from the start.
    bootstrap.fail_on = None
    calls = len(bootstrap.calls)
    await workflows.retry_workflow(store, workflow["workflow_id"])
    resumed = await _advance(store, workflow["workflow_id"])
    assert resumed["state"] == "succeeded", resumed["detail"]
    assert bootstrap.calls[calls][0] == "gpu-2"  # gpu-1 keeps the stage it finished
    with pytest.raises(workflows.WorkflowRefused):
        await workflows.retry_workflow(store, workflow["workflow_id"])


async def test_a_managed_servers_tunnel_keys_rotate_and_the_old_ones_are_revoked_last(
    store, fleet
) -> None:
    repository, bootstrap, tunnels = fleet
    await _online(store, "gpu-1")
    repository.add("gpu-1")
    await _settings(store)
    before = repository.inputs("gpu-1")
    workflow = await tunnel_key_rotation.start_tunnel_rotation(
        store, hosts=None, admin_id="admin-token"
    )

    async def beat() -> None:
        await _sql("UPDATE agent_runner_hosts SET last_heartbeat_at = NOW() + INTERVAL '1 minute'")

    await _advance(store, workflow["workflow_id"], rounds=6)
    await beat()
    final = await _advance(store, workflow["workflow_id"])
    assert final["state"] == "succeeded", final["detail"]
    assert tunnels.calls == [
        ("gateway", "authorize"),
        ("control", "authorize"),
        ("gateway", "retire"),
        ("control", "retire"),
    ]
    ((host, change),) = bootstrap.calls
    assert host == "gpu-1" and set(change["files"]) == {
        ".compute-secrets/gateway.key",
        ".compute-secrets/mcp.key",
        ".compute-secrets/control.key",
    }
    assert "runner" in change["services"]
    after = repository.inputs("gpu-1")
    assert after["public_keys"]["mcp"] != before["public_keys"]["mcp"]
    assert after["files"][".compute-secrets/mcp.key"].strip() == (
        change["files"][".compute-secrets/mcp.key"].strip()
    )
    stored = json.dumps(await store.deployments.get_workflow(workflow["workflow_id"]), default=str)
    assert "PRIVATE KEY" not in stored and 'tunnel_keys": {' not in stored.split("kind")[0]


async def test_a_far_end_nothing_here_edits_waits_for_the_administrator(store, fleet) -> None:
    repository, bootstrap, _tunnels = fleet
    await _online(store, "gpu-1")
    repository.add("gpu-1", automatic=False)
    await _settings(store)
    workflow = await tunnel_key_rotation.start_tunnel_rotation(
        store, hosts=["gpu-1"], admin_id="admin-token"
    )
    state = await _advance(store, workflow["workflow_id"], rounds=3)
    attention = state["data"]["attention"]
    assert state["state"] == "waiting" and attention["action"] == "add"
    assert "ssh-ed25519" in attention["tunnels"]["gateway"]
    assert not bootstrap.calls  # nothing switches before the far end accepts it
    with pytest.raises(workflows.WorkflowRefused):
        await tunnel_key_rotation.confirm_far_end(store, workflow["workflow_id"], "wrong")
    await tunnel_key_rotation.confirm_far_end(store, workflow["workflow_id"], attention["token"])
    await _sql("UPDATE agent_runner_hosts SET last_heartbeat_at = NOW() + INTERVAL '1 minute'")
    state = await _advance(store, workflow["workflow_id"], rounds=8)
    assert state["data"].get("attention"), (
        state["state"],
        state["detail"],
        state["data"]["phases"],
    )
    assert state["data"]["attention"]["action"] == "remove", state
    await tunnel_key_rotation.confirm_far_end(
        store, workflow["workflow_id"], state["data"]["attention"]["token"]
    )
    final = await _advance(store, workflow["workflow_id"])
    assert final["state"] == "succeeded", final["detail"]


async def test_a_self_managed_servers_tunnel_keys_use_its_agents(
    store, fleet, tmp_path, monkeypatch
) -> None:
    await _online(store, "gpu-1")
    await _settings(store)
    app = _app(store, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.api.admin_deployments.log_admin_action", lambda *a, **k: _none()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        host_store, host_exec, machine = _machine(tmp_path, "host")
        keys = tmp_path / "keys"
        keys.mkdir()
        env = "AGENT_RUNNER_HOST=gpu-1\n"
        paths = {}
        for tunnel, prefix in (
            ("gateway", "AGENT_TUNNEL"),
            ("mcp", "AGENT_MCP_TUNNEL"),
            ("control", "AGENT_CONTROL_PLANE_TUNNEL"),
        ):
            paths[tunnel] = keys / f"{tunnel}.key"
            paths[tunnel].write_text(f"old {tunnel}")
            env += f"{prefix}_SSH_KEY={paths[tunnel]}\n{prefix}_SSH_DESTINATION=t@far\n"
        (host_store.deploy_root / ".env").write_text(env)
        host_credential = await _enroll(client, host_store, "host")
        cp_store, cp_exec, _ = _machine(tmp_path, "control-plane")
        authorized = {t: tmp_path / f"{t}_authorized" for t in ("mcp", "control")}
        write_json(
            cp_store.root / "policy.json",
            {
                **cp_store.policy(),
                "tunnel_authorized_keys": {
                    t: {"path": str(p), "options": "restrict,port-forwarding"}
                    for t, p in authorized.items()
                },
            },
            0o644,
        )
        cp_credential = await _enroll(client, cp_store, "control-plane")

        async def sync() -> None:
            await _sync(client, host_exec, host_credential)
            await _sync(client, cp_exec, cp_credential)

        await sync()
        started = await client.post(
            "/admin/agent/deployments/workflows",
            json={"kind": "tunnel_keys", "confirm": "tunnel_keys"},
        )
        assert started.status_code == 200, started.text
        workflow_id = started.json()["workflow_id"]

        async def step() -> dict[str, Any]:
            await sync()
            return (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()

        state = await step()
        for _ in range(4):
            state = await step()
        # mcp and control are on the control-plane machine; the gateway is not.
        assert state["state"] == "waiting", state
        attention = state["progress"]["attention"]
        assert attention["action"] == "add" and set(attention["tunnels"]) == {"gateway"}
        assert "restrict,port-forwarding" in authorized["mcp"].read_text()
        assert not machine.calls or not any(c[0] == "ssh" for c in machine.calls)

        confirmed = await client.post(
            f"/admin/agent/deployments/workflows/{workflow_id}/confirm-far-end",
            json={"token": attention["token"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        for _ in range(3):
            state = await step()
        assert paths["mcp"].read_text().startswith("private key")
        await _sql("UPDATE agent_runner_hosts SET last_heartbeat_at = NOW() + INTERVAL '1 minute'")
        for _ in range(4):
            state = await step()
        attention = state["progress"]["attention"]
        assert attention["action"] == "remove", state
        new_key = state["progress"]["public_keys"]["gpu-1"]["new"]["mcp"]
        old_key = state["progress"]["public_keys"]["gpu-1"]["old"]["mcp"]
        assert new_key in authorized["mcp"].read_text()
        await client.post(
            f"/admin/agent/deployments/workflows/{workflow_id}/confirm-far-end",
            json={"token": attention["token"]},
        )
        for _ in range(4):
            state = await step()
        assert state["state"] == "succeeded", state
        assert old_key not in authorized["mcp"].read_text()
        assert not paths["mcp"].with_name("mcp.key.previous").exists()


async def test_a_host_agent_and_a_managed_server_rotate_together(
    store, fleet, tmp_path, monkeypatch
) -> None:
    repository, bootstrap, _tunnels = fleet
    await _online(store, "gpu-1", "self-1")
    repository.add("gpu-1")
    await _settings(store)
    app = _app(store, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.api.admin_deployments.log_admin_action", lambda *a, **k: _none()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        agent_store, executor, _ = _machine(tmp_path, "host")
        (agent_store.deploy_root / ".env").write_text(
            f"AGENT_RUNNER_HOST=self-1\nAGENT_SANDBOX_MANAGER_TOKEN={OLD}\n"
        )
        credential = await _enroll(client, agent_store, "host")
        await _sync(client, executor, credential)
        identity = agent_store.identity()
        name = "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"
        started = await client.post(
            "/admin/agent/deployments/workflows",
            json={
                "kind": "sandbox_manager_token",
                "params": {},
                "sealed_by_agent": {
                    identity["agent_id"]: {
                        name: seal(
                            identity["public_key"],
                            NEW,
                            sealed_purpose(identity["agent_id"], name),
                        )
                    }
                },
                "secret": NEW,
                "confirm": "sandbox_manager_token",
            },
        )
        assert started.status_code == 200, started.text
        bootstrap.accepts["self-1"] = {OLD, NEW}
        for _ in range(8):
            await _sync(client, executor, credential)
            state = await _advance(store, started.json()["workflow_id"], rounds=2)
        assert state["state"] == "succeeded", state
        assert key_identifier(NEW) == state["params"]["new_token_id"]


async def _none() -> None:
    return None
