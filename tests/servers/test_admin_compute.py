"""Resource API contracts with the real command service and administrator gate."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_compute
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.compute_registry import ComputeProviderRegistration, ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.ports.external.compute_provider import ProviderCapabilities
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.schemas_compute import RenewalPolicy

ADMIN = {"Authorization": "Bearer test-compute-admin"}


class MemoryResources:
    """A command-store fake: external work is never executed by HTTP handlers."""

    def __init__(self) -> None:
        self.connections: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    async def record_admin_action(self, **kwargs: Any) -> None:
        self.audit_rows.append(kwargs)

    async def list_connections(self):
        return list(self.connections.values())

    async def get_connection(self, connection_id):
        return self.connections.get(connection_id)

    async def save_connection(self, **kwargs):
        connection_id = kwargs.pop("connection_id")
        expected = kwargs.pop("expected_revision", None)
        old = self.connections.get(connection_id)
        if old and expected != old["revision"]:
            raise ValueError("revision_conflict")
        row = {"id": connection_id, **kwargs, "revision": old["revision"] + 1 if old else 1}
        self.connections[connection_id] = row
        return row

    async def list_allocations(self):
        return list(self.allocations.values())

    async def get_allocation(self, allocation_id):
        return self.allocations.get(allocation_id)

    async def list_operations(self, *, request_id=None):
        return [
            row
            for row in self.operations.values()
            if request_id is None or row["request_id"] == request_id
        ]

    async def get_operation(self, operation_id):
        return self.operations.get(operation_id)

    async def create_operation(self, **kwargs):
        for old in self.operations.values():
            if old["request_id"] == kwargs["request_id"]:
                if old["fingerprint"] != kwargs["fingerprint"]:
                    raise ValueError("idempotency_conflict")
                return old
        row = {
            "id": f"op-{len(self.operations)}",
            **kwargs,
            "state": "pending",
            "step": "queued",
            "message": "",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "result": {},
        }
        self.operations[row["id"]] = row
        return row

    async def create_allocation_operation(self, *, allocation, **kwargs):
        allocation_id = f"allocation-{len(self.allocations)}"
        row = {
            "id": allocation_id,
            "revision": 1,
            "scope_complete": False,
            "nodes": [],
            "hosts": [],
            "management_mode": "observe",
            **allocation,
        }
        self.allocations[allocation_id] = row
        return await self.create_operation(allocation_id=allocation_id, **kwargs)

    async def update_policy(
        self, *, allocation_id, expected_revision, policy, management_mode=None
    ):
        row = self.allocations[allocation_id]
        if expected_revision != row["revision"]:
            raise ValueError("revision_conflict")
        row.update(policy=policy, revision=row["revision"] + 1)
        if management_mode:
            row["management_mode"] = management_mode
        return row

    async def server_inventory(self):
        return {
            "allocations": list(self.allocations.values()),
            "hosts": [],
            "active_host": None,
            "observed_at": datetime.now(UTC),
        }

    async def retirement_impact(self, allocation_id):
        row = self.allocations[allocation_id]
        return {
            "allocation_id": allocation_id,
            "revision": row["revision"],
            "provider_id": row["provider_id"],
            "management_mode": row["management_mode"],
            "deployment_profile_configured": bool(row.get("deployment_profile")),
            "scope_complete": row["scope_complete"],
            "nodes": row["nodes"],
            "hosts": row["hosts"],
            "active_attempts": 2,
            "retained_groups": [{"id": "retained-a"}],
            "global_active_host": "host-a",
            "blocked_reasons": ["retained_groups_present"],
            "release_committed": bool(row.get("release_committed_at")),
        }

    async def retry_operation(self, operation_id):
        row = self.operations[operation_id]
        if row["state"] not in {"failed", "blocked", "unknown"}:
            raise ValueError("operation_not_retryable")
        row["state"] = "pending"
        return row

    async def cancel_operation(self, operation_id):
        row = self.operations[operation_id]
        if self.allocations[row["allocation_id"]].get("release_committed_at"):
            raise ValueError("release_committed")
        row["state"] = "cancelled"
        return row


class NoProviderCalls:
    """Any cloud side effect during an API request fails this suite."""

    def capabilities(self):
        return ProviderCapabilities(
            acquisition_modes=("attach", "provision"), can_extend=True, can_release=True
        )

    def validate_config(self, mode, config):
        return config

    async def acquire(self, *args):
        raise AssertionError("resource acquisition belongs to the worker")


@pytest.fixture
async def compute_api(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-compute-admin")
    repo = MemoryResources()
    registry = ComputeProviderRegistry(
        [
            ComputeProviderRegistration(
                id="cloudlab",
                label="CloudLab",
                provider=NoProviderCalls(),
                fields=({"key": "project", "label": "Project", "type": "text"},),
            )
        ]
    )
    configuration = ComputeConfiguration(
        observation_seconds=75,
        profiles={
            "kata": DeploymentProfile(
                id="kata",
                label="Kata",
                source_bundle="/private/operator/bundle.tar",
                source_sha256="a" * 64,
                deployment_sha="b" * 40,
                environment_ref="deployment:host-environment",
            )
        },
    )
    commands = ComputeCommands(
        repository=repo,
        registry=registry,
        secrets=ComputeSecretStore(repo, key="test-compute-key-material" * 2),
        configuration=configuration,
    )
    app = FastAPI()
    app.state.compute_commands = commands
    app.state.storage = SimpleNamespace(audit=repo)
    app.include_router(admin_compute.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, repo, app


async def connection(client):
    response = await client.post(
        "/admin/agent/compute-connections",
        headers=ADMIN,
        json={
            "name": "Research account",
            "provider_id": "cloudlab",
            "secret": "credential-canary",
            "configuration": {},
        },
    )
    assert response.status_code == 201
    return response.json()["connection"]["id"]


async def allocation(client):
    connection_id = await connection(client)
    body = {
        "name": "Experiment",
        "request_id": "request-a",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": connection_id,
        "configuration": {"project": "research"},
        "deployment_profile": "kata",
    }
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    return body, response.json()["operation"]


@pytest.mark.asyncio
async def test_every_route_requires_real_admin_gate(compute_api):
    client, _, _ = compute_api
    for route in admin_compute.router.routes:
        path = (
            route.path.replace("{connection_id}", "c")
            .replace("{allocation_id}", "a")
            .replace("{operation_id}", "o")
        )
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            response = await client.request(method, path, json={} if method != "GET" else None)
            assert response.status_code == 401, (path, response.text)
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_missing_runtime_is_unavailable_after_authentication(compute_api):
    client, _, app = compute_api
    app.state.compute_commands = None
    response = await client.get("/admin/agent/compute-providers", headers=ADMIN)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_credentials_are_encrypted_and_never_returned_or_audited(compute_api):
    client, repo, _ = compute_api
    connection_id = await connection(client)
    raw = repo.connections[connection_id]
    assert "credential-canary" not in raw["secret_ciphertext"]
    response = await client.get("/admin/agent/compute-connections", headers=ADMIN)
    assert response.status_code == 200
    assert "secret_ciphertext" not in response.text
    assert "credential-canary" not in response.text
    assert "credential-canary" not in str(repo.audit_rows)
    assert repo.audit_rows[0]["actor_id"] == "admin-token"
    assert repo.audit_rows[0]["details"] == {"connection_id": connection_id}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_server_access_projection_links_credentials_without_transport_secrets(compute_api):
    client, repo, _ = compute_api
    body, operation = await allocation(client)
    row = repo.allocations[operation["allocation_id"]]
    row["ssh_connection_id"] = "ssh-access-id"
    row["nodes"] = [
        {
            "id": "node-a",
            "name": "Research node",
            "connection": {
                "address": "node.example",
                "port": 2222,
                "username": "researcher",
                "credential_ref": "private-reference-canary",
                "known_hosts_ref": "private-known-hosts-canary",
            },
            "instance_identity": "private-identity-canary",
        }
    ]
    row["configuration"]["extra"] = "private-config-canary"
    row["hosts"] = [
        {"host": "host-a", "compute_node_id": "node-a", "credential_hash": "private-hash-canary"}
    ]
    response = await client.get("/admin/agent/servers", headers=ADMIN)
    assert response.status_code == 200
    public = response.json()["allocations"][0]
    assert public["connection_id"] == body["connection_id"]
    assert public["ssh_connection_id"] == "ssh-access-id"
    assert public["nodes"] == [
        {
            "id": "node-a",
            "name": "Research node",
            "hosts": ["host-a"],
            "address": "node.example",
            "port": 2222,
            "username": "researcher",
        }
    ]
    assert "canary" not in response.text
    assert "configuration" not in public
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"name": "Bad", "provider_id": "cloudlab", "secret": {"token": "credential-canary"}},
        {
            "name": "Bad",
            "provider_id": "cloudlab",
            "secret": "credential-canary",
            "unexpected": "credential-canary",
        },
    ],
)
async def test_validation_never_echoes_secret_input(compute_api, body):
    client, repo, _ = compute_api
    response = await client.post("/admin/agent/compute-connections", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert "credential-canary" not in response.text
    assert "input" not in response.text
    assert not repo.connections
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_malformed_json_does_not_echo_credentials(compute_api):
    client, _, _ = compute_api
    response = await client.post(
        "/admin/agent/compute-connections",
        headers={**ADMIN, "Content-Type": "application/json"},
        content='{"secret":"credential-canary",',
    )
    assert response.status_code == 422
    assert "credential-canary" not in response.text


@pytest.mark.asyncio
async def test_request_identity_recovers_one_operation_and_refuses_changed_inputs(compute_api):
    client, repo, _ = compute_api
    body, original = await allocation(client)
    replay = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert replay.status_code == 202
    assert replay.json()["operation"]["id"] == original["id"]
    assert len(repo.allocations) == 1
    assert len(repo.operations) == 1
    result = await client.get("/admin/agent/compute-operations?request_id=request-a", headers=ADMIN)
    assert result.json()["operations"][0]["id"] == original["id"]
    assert "parameters" not in result.text
    assert "private/operator" not in result.text
    conflict = await client.post(
        "/admin/agent/servers", headers=ADMIN, json={**body, "name": "Changed"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert len(repo.allocations) == 1


@pytest.mark.asyncio
async def test_automatic_server_setup_replay_keeps_original_profile(compute_api):
    client, repo, app = compute_api
    body = {
        "name": "Automatic setup",
        "request_id": "automatic-a",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": await connection(client),
        "configuration": {"project": "research"},
    }
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    operation = response.json()["operation"]
    profile = repo.allocations[operation["allocation_id"]]["deployment_profile"]
    assert profile["id"] == "kata"
    assert repo.operations[operation["id"]]["parameters"]["deployment_profile"] is None

    # The persisted allocation owns the chosen setup, including if configuration
    # changes before the client recovers an ambiguous response.
    app.state.compute_commands.configuration.profiles.clear()
    replay = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation"]["id"] == operation["id"]
    assert len(repo.allocations) == len(repo.operations) == 1
    assert repo.allocations[operation["allocation_id"]]["deployment_profile"] == profile
    conflict = await client.post(
        "/admin/agent/servers", headers=ADMIN, json={**body, "name": "Changed"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_input", [False, True])
async def test_concurrent_setup_selection_recovers_duplicate_before_capacity_refusal(
    compute_api, monkeypatch, changed_input
):
    client, repo, app = compute_api
    commands = app.state.compute_commands
    configure = commands._configuration
    allocate = repo.create_allocation_operation
    both_requests_entered = asyncio.Event()
    first_allocated = asyncio.Event()
    entered = 0

    async def interleaved_configuration(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 1:
            await both_requests_entered.wait()
        else:
            both_requests_entered.set()
            await first_allocated.wait()
        return await configure(*args, **kwargs)

    async def signal_allocation(**kwargs):
        operation = await allocate(**kwargs)
        first_allocated.set()
        return operation

    monkeypatch.setattr(commands, "_configuration", interleaved_configuration)
    monkeypatch.setattr(repo, "create_allocation_operation", signal_allocation)
    body = {
        "name": "Concurrent setup",
        "request_id": "automatic-concurrent",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": await connection(client),
    }
    second_body = {**body, "name": "Changed"} if changed_input else body
    first, second = await asyncio.wait_for(
        asyncio.gather(
            client.post("/admin/agent/servers", headers=ADMIN, json=body),
            client.post("/admin/agent/servers", headers=ADMIN, json=second_body),
        ),
        timeout=5,
    )
    assert entered == 2
    assert first.status_code == 202, first.text
    if changed_input:
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "idempotency_conflict"
    else:
        assert second.status_code == 202, second.text
        assert second.json()["operation"]["id"] == first.json()["operation"]["id"]
    assert len(repo.allocations) == len(repo.operations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "ready", "failed", "unknown"])
async def test_automatic_server_setup_skips_live_profile(compute_api, state):
    client, repo, app = compute_api
    body, existing = await allocation(client)
    repo.allocations[existing["allocation_id"]]["observed_state"] = state
    profiles = app.state.compute_commands.configuration.profiles
    profiles["second"] = profiles["kata"].model_copy(
        update={"id": "second", "environment_ref": "deployment:second-host-environment"}
    )
    del body["deployment_profile"]
    body["request_id"] = "automatic-second"
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    created = repo.allocations[response.json()["operation"]["allocation_id"]]
    assert created["deployment_profile"] == profiles["second"].model_dump(mode="json")
    assert len(repo.allocations) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["terminated", "absent"])
async def test_automatic_server_setup_reuses_only_confirmed_released_profile(compute_api, state):
    client, repo, _ = compute_api
    body, existing = await allocation(client)
    repo.allocations[existing["allocation_id"]]["observed_state"] = state
    del body["deployment_profile"]
    body["request_id"] = "automatic-replacement"
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    created = repo.allocations[response.json()["operation"]["allocation_id"]]
    assert created["deployment_profile"]["id"] == "kata"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_automatic_server_setup_refuses_without_available_capacity(compute_api, configured):
    client, repo, app = compute_api
    body, _ = await allocation(client)
    if not configured:
        app.state.compute_commands.configuration.profiles.clear()
    del body["deployment_profile"]
    body["request_id"] = "automatic-unavailable"
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == (409 if configured else 422)
    assert response.json()["detail"]["code"] == "profile_unavailable"
    assert len(repo.allocations) == len(repo.operations) == 1


@pytest.mark.asyncio
async def test_automatic_preflight_uses_setup_without_reserving_it(compute_api):
    client, repo, _ = compute_api
    body = {
        "name": "Preflight",
        "request_id": "automatic-preflight",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": await connection(client),
    }
    response = await client.post("/admin/agent/server-preflights", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation"]["id"]
    assert repo.operations[operation_id]["parameters"]["profile"]["id"] == "kata"
    assert not repo.allocations
    body["request_id"] = "automatic-after-preflight"
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    created = repo.allocations[response.json()["operation"]["allocation_id"]]
    assert created["deployment_profile"]["id"] == "kata"


@pytest.mark.asyncio
async def test_automatic_server_setup_preserves_repository_race_refusal(compute_api, monkeypatch):
    client, repo, _ = compute_api

    async def occupied_after_selection(**kwargs):
        assert kwargs["allocation"]["deployment_profile"]["id"] == "kata"
        raise ValueError("deployment_profile_in_use")

    monkeypatch.setattr(repo, "create_allocation_operation", occupied_after_selection)
    response = await client.post(
        "/admin/agent/servers",
        headers=ADMIN,
        json={
            "name": "Racing addition",
            "request_id": "automatic-race",
            "provider_id": "cloudlab",
            "mode": "provision",
            "connection_id": await connection(client),
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "deployment_profile_in_use"
    assert not repo.allocations
    assert not repo.operations


@pytest.mark.asyncio
async def test_preflight_does_not_create_allocation(compute_api):
    client, repo, _ = compute_api
    connection_id = await connection(client)
    response = await client.post(
        "/admin/agent/server-preflights",
        headers=ADMIN,
        json={
            "name": "Check",
            "request_id": "preflight-a",
            "provider_id": "cloudlab",
            "mode": "provision",
            "connection_id": connection_id,
            "configuration": {"project": "research"},
            "deployment_profile": "kata",
        },
    )
    assert response.status_code == 202
    assert response.json()["operation"]["kind"] == "preflight"
    assert not repo.allocations
    assert "private/operator" not in response.text


@pytest.mark.asyncio
async def test_rotation_checks_revision_and_marks_credential_unverified(compute_api):
    client, repo, _ = compute_api
    connection_id = await connection(client)
    response = await client.patch(
        f"/admin/agent/compute-connections/{connection_id}",
        headers=ADMIN,
        json={"expected_revision": 1, "secret": "rotated-canary"},
    )
    assert response.status_code == 200
    assert response.json()["connection"]["revision"] == 2
    assert response.json()["connection"]["status"] == "unverified"
    assert "rotated-canary" not in response.text
    conflict = await client.patch(
        f"/admin/agent/compute-connections/{connection_id}",
        headers=ADMIN,
        json={"expected_revision": 1, "secret": "stale-canary"},
    )
    assert conflict.status_code == 409
    assert repo.connections[connection_id]["revision"] == 2
    assert "stale-canary" not in conflict.text


@pytest.mark.asyncio
async def test_policy_is_revisioned_and_does_not_call_extend(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    allocation_id = operation["allocation_id"]
    repo.allocations[allocation_id]["scope_complete"] = True
    policy = RenewalPolicy(enabled=True, reason="Finish evaluation").model_dump()
    response = await client.patch(
        f"/admin/agent/compute-allocations/{allocation_id}/policy",
        headers=ADMIN,
        json={"expected_revision": 1, "policy": policy},
    )
    assert response.status_code == 200
    assert response.json()["allocation"]["policy"]["enabled"] is True
    assert len(repo.operations) == 1
    conflict = await client.patch(
        f"/admin/agent/compute-allocations/{allocation_id}/policy",
        headers=ADMIN,
        json={"expected_revision": 1, "policy": policy},
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_impact_whitelists_scope_and_irreversible_release_disables_actions(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    row = repo.allocations[operation["allocation_id"]]
    row["nodes"] = [
        {"id": "node-a", "name": "Machine A", "connection": {"credential_ref": "private-reference"}}
    ]
    row["hosts"] = [
        {"host": "host-a", "compute_node_id": "node-a", "credential_hash": "private-hash"}
    ]
    response = await client.get(
        f"/admin/agent/compute-allocations/{row['id']}/retirement-impact", headers=ADMIN
    )
    assert response.status_code == 200
    assert response.json()["retained_groups"] == 1
    assert response.json()["hosts"] == ["host-a"]
    assert response.json()["revision"] == row["revision"]
    assert response.json()["allowed_actions"] == ["retire"]
    assert "private-reference" not in response.text
    assert "private-hash" not in response.text
    repo.operations[operation["id"]].update(kind="retire", state="unknown")
    row["release_committed_at"] = datetime.now(UTC)
    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)
    assert observed.json()["operation"]["allowed_actions"] == ["retry"]
    cancel = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/cancel", headers=ADMIN, json={}
    )
    assert cancel.status_code == 409


@pytest.mark.asyncio
async def test_catalog_exposes_no_deployment_secret_paths(compute_api):
    client, _, _ = compute_api
    response = await client.get("/admin/agent/compute-providers", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["deployment_profiles"] == [{"id": "kata", "label": "Kata"}]
    assert "private/operator" not in response.text
    assert "environment_ref" not in response.text


@pytest.mark.asyncio
async def test_unresolved_external_result_returns_safe_conflict(compute_api, monkeypatch):
    """Storage fencing stays an actionable refusal when a prior provider call is unknown."""
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.operations[operation["id"]].update(
        kind="retire", state="blocked", step="awaiting_reconciliation"
    )

    async def refuse_cancel(operation_id):
        assert operation_id == operation["id"]
        raise ValueError("external_operation_unresolved")

    monkeypatch.setattr(repo, "cancel_operation", refuse_cancel)
    response = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/cancel", headers=ADMIN, json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "external_operation_unresolved",
        "message": "The external operation is unresolved; reconcile its result before continuing.",
    }
    assert response.headers["cache-control"] == "no-store"
    assert repo.operations[operation["id"]]["state"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "external_id", "provider_id", "expected"),
    [
        ("ready", "experiment-a", "cloudlab", 75),
        ("unknown", None, "cloudlab", None),
        ("terminated", "experiment-a", "cloudlab", None),
        ("absent", "experiment-a", "cloudlab", None),
        ("ready", "experiment-a", "unconfigured", None),
    ],
)
async def test_inventory_reports_configured_period_only_for_observable_resources(
    compute_api, state, external_id, provider_id, expected
):
    """A periodic interval is configuration, not a promised next execution timestamp."""
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.allocations[operation["allocation_id"]].update(
        observed_state=state, external_id=external_id, provider_id=provider_id
    )
    response = await client.get("/admin/agent/servers", headers=ADMIN)
    assert response.status_code == 200
    resource = response.json()["allocations"][0]
    assert resource["check_interval_seconds"] == expected
    assert "next_check_at" not in resource
