"""Resource API contracts with the real command service and administrator gate."""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_compute
from cloud_agent.application.cloudlab_inventory import CloudLabInventoryUnavailable
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.compute_registry import ComputeProviderRegistration, ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.domain.cloudlab import DEFAULT_CLOUDLAB_NODE_TYPES
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.ports.external.compute_provider import (
    UNCHECKED_CLOUDLAB_HOST_KEY_REF,
    ProviderCapabilities,
)
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.schemas_compute import AutomaticDeploymentSetup, RenewalPolicy
from cloud_agent_shared.egress import network_from_env

ADMIN = {"Authorization": "Bearer test-compute-admin"}


class MemoryResources:
    """A command-store fake: external work is never executed by HTTP handlers."""

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.reserved_profiles: set[str] = set()
        self.autoscaling = {
            "policy": {
                "revision": 0,
                "enabled": False,
                "min_hosts": 0,
                "max_hosts": 0,
                "connection_id": None,
                "ssh_connection_id": None,
                "configuration": {},
                "deployment_profiles": [],
                "sandbox_storage_path": None,
                "renewal_policy": {},
                "slot_policy": {},
                "scale_up_sustain_seconds": 120,
                "scale_down_idle_seconds": 3600,
                "cooldown_seconds": 600,
                "scale_down_enabled": False,
                "created_at": now,
                "updated_at": now,
            },
            "runtime": {
                "generation": 0,
                "pressure_since": None,
                "idle_since": None,
                "cooldown_until": None,
                "queue_pressure": 0,
                "slot_status": [],
                "last_decision": {},
                "last_decision_at": None,
                "blocker_code": None,
                "blocker_message": None,
                "observed_at": None,
                "updated_at": now,
            },
            "members": [],
            "profile_reservations": [],
            "observed_at": now,
        }
        self.profiles: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.defaults: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    async def list_deployment_profiles(self):
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"source_bundle", "secret_ciphertext"}
            }
            for row in self.profiles.values()
        ]

    async def get_deployment_profile(self, profile_id):
        return self.profiles.get(profile_id)

    async def save_deployment_profile(self, **kwargs):
        profile_id = kwargs.pop("profile_id")
        if profile_id in self.profiles:
            raise ValueError("deployment_profile_exists")
        row = {"id": profile_id, **kwargs, "revision": 1}
        self.profiles[profile_id] = row
        return row

    async def record_admin_action(self, **kwargs: Any) -> None:
        self.audit_rows.append(kwargs)

    async def provider_defaults(self, provider_id):
        return self.defaults.get(provider_id, {})

    async def set_provider_defaults(self, provider_id, defaults):
        self.defaults[provider_id] = defaults
        return defaults

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

    async def get_cloudlab_autoscaling(self):
        return self.autoscaling

    async def update_cloudlab_autoscaling(
        self, *, expected_revision, policy, profile_snapshots, requested_by
    ):
        if expected_revision != self.autoscaling["policy"]["revision"]:
            raise ValueError("revision_conflict")
        revision = expected_revision + 1
        self.reserved_profiles.update(policy["deployment_profiles"])
        self.autoscaling["policy"] = {
            **policy,
            "revision": revision,
            "created_at": self.autoscaling["policy"]["created_at"],
            "updated_at": datetime.now(UTC),
        }
        self.autoscaling["profile_reservations"] = [
            {
                "deployment_profile": profile,
                "allocation_id": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            for profile in sorted(self.reserved_profiles)
        ]
        return self.autoscaling

    async def list_reserved_deployment_profiles(self):
        return sorted(self.reserved_profiles)

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
        allocation = self.allocations.get(row.get("allocation_id"))
        if (
            allocation
            and allocation.get("release_committed_at")
            and row["kind"] not in {"retire", "inspect", "discard"}
        ):
            raise ValueError("operation_not_retryable")
        if (
            row["kind"] == "acquire"
            and allocation
            and allocation.get("autoscaling_owned")
            and allocation.get("observed_state") in {"terminated", "absent"}
        ):
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
                fields=(
                    {"key": "project", "label": "Project", "type": "text"},
                    {"key": "duration", "label": "Duration", "type": "number"},
                    {"key": "profile_project", "label": "Profile project", "type": "text"},
                    {"key": "profile_name", "label": "Profile name", "type": "text"},
                    {
                        "key": "node_types",
                        "label": "Preferred node types",
                        "type": "string_list",
                        "modes": ["provision"],
                    },
                    {
                        "key": "node_type_binding",
                        "label": "Node type parameter",
                        "type": "text",
                        "modes": ["provision"],
                    },
                    {
                        "key": "ssh_username",
                        "label": "SSH user",
                        "type": "text",
                        "deployment_only": True,
                    },
                ),
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
    cloudlab_inventory = SimpleNamespace(
        preferred_nodes=AsyncMock(
            return_value={
                "nodes": [
                    {
                        "node_type": "c6620",
                        "site": "Utah",
                        "available": 54,
                        "total": 132,
                        "introduced_year": 2025,
                        "physical_cores": 28,
                        "logical_threads": 56,
                        "memory_gib": 128,
                    }
                ],
                "observed_at": "2026-10-11T12:00:00+00:00",
                "source": "CloudLab public hardware inventory",
                "filters": {
                    "total_greater_than": 20,
                    "minimum_introduced_year": 2021,
                    "minimum_physical_cores": 16,
                    "minimum_memory_gib": 128,
                    "general_purpose_only": True,
                },
            }
        )
    )
    commands = ComputeCommands(
        repository=repo,
        registry=registry,
        secrets=ComputeSecretStore(repo, key="test-compute-key-material" * 2),
        configuration=configuration,
        cloudlab_inventory=cast("Any", cloudlab_inventory),
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
async def test_remove_is_available_when_provider_is_unconfigured(compute_api):
    client, _, app = compute_api
    _, operation = await allocation(client)
    app.state.compute_commands.registry = ComputeProviderRegistry([])
    app.state.compute_commands.queries.registry = app.state.compute_commands.registry

    response = await client.get("/admin/agent/servers", headers=ADMIN)

    assert response.status_code == 200, response.text
    server = next(
        row for row in response.json()["allocations"] if row["id"] == operation["allocation_id"]
    )
    assert server["allowed_actions"] == ["remove"]


@pytest.mark.asyncio
async def test_remove_server_uses_reviewed_revision_and_audits_identifiers_only(
    compute_api, monkeypatch
):
    client, repo, _ = compute_api
    remove = AsyncMock()
    monkeypatch.setattr(repo, "remove_allocation", remove, raising=False)

    response = await client.request(
        "DELETE",
        "/admin/agent/servers/allocation-a",
        headers=ADMIN,
        json={"expected_revision": 4},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"removed": True}
    assert response.headers["cache-control"] == "no-store"
    remove.assert_awaited_once_with(allocation_id="allocation-a", expected_revision=4)
    assert len(repo.audit_rows) == 1
    assert repo.audit_rows[0]["action"] == "agent.compute.server.remove"
    assert repo.audit_rows[0]["details"] == {"allocation_id": "allocation-a"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("revision_conflict", 409),
        ("allocation_not_found", 404),
        ("resource_release_required", 409),
        ("external_operation_unresolved", 409),
        ("retained_groups_present", 409),
        ("placement_pinned", 409),
        ("deployment_drain_active", 409),
        ("active_attempts_present", 409),
    ],
)
async def test_remove_server_reports_safe_refusals_without_auditing(
    compute_api, monkeypatch, code, status
):
    client, repo, _ = compute_api
    remove = AsyncMock(side_effect=ValueError(code))
    monkeypatch.setattr(repo, "remove_allocation", remove, raising=False)

    response = await client.request(
        "DELETE",
        "/admin/agent/servers/allocation-a",
        headers=ADMIN,
        json={"expected_revision": 4},
    )

    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert repo.audit_rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"expected_revision": -1}, {"expected_revision": 1, "force": True}]
)
async def test_remove_server_requires_a_valid_reviewed_revision(compute_api, monkeypatch, payload):
    client, repo, _ = compute_api
    remove = AsyncMock()
    monkeypatch.setattr(repo, "remove_allocation", remove, raising=False)

    response = await client.request(
        "DELETE", "/admin/agent/servers/allocation-a", headers=ADMIN, json=payload
    )

    assert response.status_code == 422
    remove.assert_not_awaited()
    assert repo.audit_rows == []


@pytest.mark.asyncio
async def test_cloudlab_autoscaling_revision_zero_is_safe_and_defaulted(compute_api):
    client, _, _ = compute_api

    response = await client.get("/admin/agent/compute-autoscaling/cloudlab", headers=ADMIN)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy"]["revision"] == 0
    assert body["policy"]["enabled"] is False
    assert body["policy"]["connection_id"] is None
    assert body["policy"]["ssh_connection_id"] is None
    assert body["policy"]["deployment_profiles"] == []
    assert body["policy"]["slot_policy"] == {
        "formula_version": 1,
        "cpu_reservation_fraction": 0.75,
        "cpu_per_slot": 2,
        "memory_reserve_gib": 8,
        "memory_per_slot_gib": 4,
        "min_slots": 2,
        "max_slots": 128,
    }
    assert "created_at" not in body["policy"]
    assert body["runtime"]["last_decision"] == {}
    assert body["members"] == []
    assert body["profile_reservations"] == []


@pytest.mark.asyncio
async def test_cloudlab_preferred_nodes_are_admin_only_safe_and_not_cached(compute_api):
    client, _, app = compute_api

    response = await client.get("/admin/agent/compute-availability/cloudlab", headers=ADMIN)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["nodes"] == [
        {
            "node_type": "c6620",
            "site": "Utah",
            "available": 54,
            "total": 132,
            "introduced_year": 2025,
            "physical_cores": 28,
            "logical_threads": 56,
            "memory_gib": 128,
        }
    ]
    assert "credential" not in response.text.lower()
    app.state.compute_commands.cloudlab_inventory.preferred_nodes.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cloudlab_preferred_nodes_fail_closed_with_safe_error(compute_api):
    client, _, app = compute_api
    app.state.compute_commands.cloudlab_inventory.preferred_nodes.side_effect = (
        CloudLabInventoryUnavailable()
    )

    response = await client.get("/admin/agent/compute-availability/cloudlab", headers=ADMIN)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "cloudlab_availability_unavailable",
        "message": "CloudLab node availability is temporarily unavailable.",
    }


@pytest.mark.asyncio
async def test_cloudlab_autoscaling_v1_rejects_rotation_enabled(compute_api):
    client, repo, _ = compute_api

    response = await client.put(
        "/admin/agent/compute-autoscaling/cloudlab",
        headers=ADMIN,
        json={
            "expected_revision": 0,
            "policy": {
                "renewal_policy": {
                    "enabled": True,
                    "reason": "Keep autoscaled staging capacity available",
                    "renew_before_seconds": 3 * 3600,
                    "extend_to_horizon_seconds": 8 * 3600,
                    "rotation_enabled": True,
                    "node_types": ["m510"],
                }
            },
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_configuration"
    assert repo.autoscaling["policy"]["revision"] == 0


@pytest.mark.asyncio
async def test_cloudlab_autoscaling_put_is_canonical_revisioned_audited_and_pausable(
    compute_api,
):
    client, repo, app = compute_api
    commands = app.state.compute_commands
    cloudlab = commands.registry.get("cloudlab")
    commands.registry = ComputeProviderRegistry(
        [
            ComputeProviderRegistration(
                id=cloudlab.id,
                label=cloudlab.label,
                provider=cloudlab.provider,
                ssh_credential_fields=("ssh_credential_ref", "ssh_known_hosts_ref"),
                fields=(
                    *cloudlab.fields,
                    {
                        "key": "host_key_source",
                        "label": "Host-key source",
                        "type": "select",
                    },
                ),
            )
        ]
    )
    repo.connections.update(
        {
            "cloudlab-verified": {
                "id": "cloudlab-verified",
                "name": "CloudLab",
                "provider_id": "cloudlab",
                "configuration": {},
                "secret_ciphertext": "cloudlab-private-canary",
                "masked_summary": "configured",
                "status": "verified",
                "revision": 1,
            },
            "ssh-verified": {
                "id": "ssh-verified",
                "name": "SSH",
                "provider_id": "existing_ssh",
                "configuration": {},
                "secret_ciphertext": "ssh-private-canary",
                "masked_summary": "configured",
                "status": "verified",
                "revision": 1,
            },
        }
    )
    policy = {
        "enabled": True,
        "min_hosts": 1,
        "max_hosts": 1,
        "connection_id": "cloudlab-verified",
        "ssh_connection_id": "ssh-verified",
        "configuration": {
            "project": "research",
            "profile_project": "templates",
            "profile_name": "worker",
            "duration": 8,
            "ssh_username": "ubuntu",
            "host_key_source": "registration",
        },
        "deployment_profiles": ["kata"],
        "sandbox_storage_path": "/mnt/cloud-agent",
        "scale_up_sustain_seconds": 30,
        "scale_down_idle_seconds": 900,
        "cooldown_seconds": 120,
        "scale_down_enabled": True,
    }

    response = await client.put(
        "/admin/agent/compute-autoscaling/cloudlab",
        headers=ADMIN,
        json={"expected_revision": 0, "policy": policy},
    )

    assert response.status_code == 200, response.text
    saved = response.json()["policy"]
    assert saved["revision"] == 1
    assert saved["configuration"] == {
        **policy["configuration"],
        "node_types": list(DEFAULT_CLOUDLAB_NODE_TYPES),
        "node_type_binding": "phystype",
    }
    assert "credential_ref" not in response.text
    assert "private-canary" not in response.text
    assert repo.autoscaling["profile_reservations"][0]["deployment_profile"] == "kata"
    assert repo.audit_rows[-1]["action"] == "agent.compute.cloudlab_autoscaling.set"
    assert repo.audit_rows[-1]["actor_id"] == "admin-token"
    assert repo.audit_rows[-1]["details"] == {}

    conflict = await client.put(
        "/admin/agent/compute-autoscaling/cloudlab",
        headers=ADMIN,
        json={"expected_revision": 0, "policy": policy},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "revision_conflict"

    repo.connections["cloudlab-verified"]["status"] = "unverified"
    repo.connections["ssh-verified"]["status"] = "unverified"
    paused_policy = {key: value for key, value in saved.items() if key != "revision"}
    paused_policy["enabled"] = False
    commands.deployment_profiles = AsyncMock(
        side_effect=AssertionError("an exact pause must not load deployment profiles")
    )
    paused = await client.put(
        "/admin/agent/compute-autoscaling/cloudlab",
        headers=ADMIN,
        json={"expected_revision": 1, "policy": paused_policy},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["policy"]["revision"] == 2
    assert paused.json()["policy"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_manual_server_cannot_take_autoscaling_reserved_profile(compute_api, explicit):
    client, repo, _ = compute_api
    repo.reserved_profiles.add("kata")
    body = {
        "name": "Manual",
        "request_id": f"manual-reserved-{explicit}",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": await connection(client),
        "configuration": {"project": "research"},
    }
    if explicit:
        body["deployment_profile"] = "kata"

    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "deployment_profile_reserved" if explicit else "profile_unavailable"
    )
    assert repo.allocations == {}


def automatic_setup_request() -> dict[str, Any]:
    """Use infrastructure descriptors without including deployment credentials."""
    return {
        "request_id": "automatic-setup-once",
        "gateway": {
            "connection_id": "gateway-key",
            "address": "gateway-input-canary.example.org",
            "username": "gateway_operator",
        },
        "control": {
            "connection_id": "control-key",
            "address": "control-input-canary.example.org",
            "username": "control_operator",
        },
        "chat_network_pool": "172.29.0.0/16",
        "dns_server": "1.1.1.1",
        "protected_cidrs": ["198.51.100.0/24", "192.0.2.0/24"],
    }


@pytest.mark.asyncio
async def test_automatic_setup_readiness_is_awaited_and_not_audited(compute_api):
    client, repo, app = compute_api
    status = {
        "available": True,
        "deployment_sha": "c" * 40,
        "blocked_reasons": [],
        "defaults": AutomaticDeploymentSetup.model_validate(automatic_setup_request()).model_dump(
            mode="json", exclude={"request_id"}
        ),
        "pending_request_id": "automatic-setup-once",
    }
    service = SimpleNamespace(read_status=AsyncMock(return_value=status), create=AsyncMock())
    app.state.compute_commands.automatic_setup = service

    response = await client.get("/admin/agent/compute-automatic-setup", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == status
    assert response.headers["cache-control"] == "no-store"
    service.read_status.assert_awaited_once_with()
    service.create.assert_not_awaited()
    assert repo.audit_rows == []


@pytest.mark.asyncio
async def test_automatic_setup_parses_targets_and_audits_only_profile_ids(compute_api):
    client, repo, app = compute_api
    public_result = {
        "profiles": [
            {"id": "automatic-primary", "label": "Primary"},
            {"id": "automatic-spare", "label": "Spare"},
        ]
    }
    service = SimpleNamespace(
        read_status=AsyncMock(),
        create=AsyncMock(return_value=public_result),
        broker_token="broker-token-canary",
        private_key="generated-private-key-canary",
    )
    app.state.compute_commands.automatic_setup = service
    body = automatic_setup_request()

    response = await client.post(
        "/admin/agent/compute-deployment-profiles/automatic", headers=ADMIN, json=body
    )

    assert response.status_code == 201, response.text
    assert response.json() == public_result
    assert response.headers["cache-control"] == "no-store"
    service.create.assert_awaited_once()
    parsed = service.create.await_args.args[0]
    assert isinstance(parsed, AutomaticDeploymentSetup)
    assert parsed.request_id == body["request_id"]
    assert parsed.gateway.connection_id == "gateway-key"
    assert parsed.gateway.address == body["gateway"]["address"]
    assert parsed.gateway.username == "gateway_operator"
    assert parsed.gateway.port == 22
    assert parsed.control.connection_id == "control-key"
    assert parsed.control.address == body["control"]["address"]
    assert parsed.control.username == "control_operator"
    assert parsed.control.port == 22
    assert parsed.gateway_port == 8080
    assert parsed.slots == 2
    assert parsed.protected_cidrs == ["192.0.2.0/24", "198.51.100.0/24"]
    assert len(repo.audit_rows) == 1
    assert repo.audit_rows[0]["action"] == "agent.compute.profile.automatic_create"
    assert repo.audit_rows[0]["actor_id"] == "admin-token"
    assert repo.audit_rows[0]["details"] == {"profile_id": "automatic-primary,automatic-spare"}
    for canary in (
        body["gateway"]["address"],
        body["control"]["address"],
        service.broker_token,
        service.private_key,
    ):
        assert canary not in response.text
        assert canary not in str(repo.audit_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"dns_server": "172.29.0.53"},
        {"chat_network_pool": "203.0.113.0/24"},
        {"protected_cidrs": ["invalid-network-canary"]},
    ],
    ids=["dns-in-pool", "public-pool", "malformed-protected"],
)
async def test_automatic_setup_network_validation_precedes_external_work(compute_api, invalid):
    client, repo, app = compute_api
    service = SimpleNamespace(read_status=AsyncMock(), create=AsyncMock())
    app.state.compute_commands.automatic_setup = service
    body = {**automatic_setup_request(), **invalid}

    response = await client.post(
        "/admin/agent/compute-deployment-profiles/automatic", headers=ADMIN, json=body
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_configuration",
            "message": "Check the required fields and configuration values.",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert "canary" not in response.text
    service.create.assert_not_awaited()
    assert repo.audit_rows == []


@pytest.mark.asyncio
async def test_automatic_setup_accepts_host_supported_broad_protected_networks(compute_api):
    client, _, app = compute_api
    service = SimpleNamespace(
        read_status=AsyncMock(),
        create=AsyncMock(return_value={"profiles": [{"id": "broad-protected", "label": "Kata"}]}),
    )
    app.state.compute_commands.automatic_setup = service
    body = {
        **automatic_setup_request(),
        "protected_cidrs": ["172.16.0.0/12", "10.0.0.0/8"],
    }

    response = await client.post(
        "/admin/agent/compute-deployment-profiles/automatic", headers=ADMIN, json=body
    )

    assert response.status_code == 201, response.text
    service.create.assert_awaited_once()
    parsed = service.create.await_args.args[0]
    network = network_from_env(
        {
            "AGENT_SANDBOX_NAMESPACE": "ca-broad-protected",
            "AGENT_RELAY_NETWORK": "relay-broad-protected",
            "AGENT_GATEWAY_URL": "http://inference-relay:8080",
            "AGENT_SANDBOX_MCP_URL": "http://mcp-relay:8001",
            "AGENT_EGRESS_CHAT_NETWORK_POOL": parsed.chat_network_pool,
            "AGENT_EGRESS_DNS_SERVER": parsed.dns_server,
            "AGENT_EGRESS_PROTECTED_CIDRS": ",".join(parsed.protected_cidrs),
        }
    )
    assert str(network.pool) == body["chat_network_pool"]
    assert [str(cidr) for cidr in network.extra_protected] == parsed.protected_cidrs
    assert any(network.pool.subnet_of(cidr) for cidr in network.extra_protected)


@pytest.mark.asyncio
async def test_automatic_setup_refusal_returns_safe_503_without_input_or_audit(compute_api):
    client, repo, app = compute_api
    service = SimpleNamespace(
        read_status=AsyncMock(),
        create=AsyncMock(
            side_effect=ComputeRefusal(
                "automatic_release_unavailable",
                "Automatic setup needs the Host bundle included in this release.",
                status=503,
            )
        ),
    )
    app.state.compute_commands.automatic_setup = service

    response = await client.post(
        "/admin/agent/compute-deployment-profiles/automatic",
        headers=ADMIN,
        json=automatic_setup_request(),
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "automatic_release_unavailable",
            "message": "Automatic setup needs the Host bundle included in this release.",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert "canary" not in response.text
    service.create.assert_awaited_once()
    assert repo.audit_rows == []


@pytest.mark.asyncio
async def test_cloudlab_defaults_are_saved_audited_and_exposed_in_the_catalog(compute_api):
    client, repo, _ = compute_api
    defaults = {
        "project": "mad-cloudlab",
        "duration": 12,
        "profile_project": "cloud-agent",
        "profile_name": "kata-node",
        "ssh_username": "researcher",
    }

    initial = await client.get("/admin/agent/compute-defaults/cloudlab", headers=ADMIN)
    assert initial.status_code == 200
    assert initial.json()["defaults"]["duration"] == 4
    assert initial.json()["defaults"]["node_types"] == list(DEFAULT_CLOUDLAB_NODE_TYPES)
    assert initial.json()["defaults"]["node_type_binding"] == "phystype"

    saved = await client.put("/admin/agent/compute-defaults/cloudlab", headers=ADMIN, json=defaults)
    assert saved.status_code == 200
    effective = {
        **defaults,
        "ssh_public_key": "",
        "node_types": list(DEFAULT_CLOUDLAB_NODE_TYPES),
        "node_type_binding": "phystype",
    }
    assert saved.json() == {"defaults": effective}
    assert repo.defaults == {"cloudlab": effective}
    assert repo.audit_rows[-1]["action"] == "agent.compute.cloudlab_defaults.set"
    assert repo.audit_rows[-1]["details"] == {}

    catalog = (await client.get("/admin/agent/compute-providers", headers=ADMIN)).json()
    fields = {field["key"]: field for field in catalog["providers"][0]["fields"]}
    assert {key: fields[key]["default"] for key in defaults} == defaults


@pytest.mark.asyncio
async def test_cloudlab_defaults_accept_one_ssh_public_key_for_the_ssh_user(compute_api):
    client, repo, _ = compute_api
    public_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    saved = await client.put(
        "/admin/agent/compute-defaults/cloudlab",
        headers=ADMIN,
        json={"ssh_username": "researcher", "ssh_public_key": f"{public_key}  researcher@lab"},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["defaults"]["ssh_public_key"] == f"{public_key} researcher@lab"
    assert saved.json()["defaults"]["ssh_username"] == "researcher"
    assert repo.audit_rows[-1]["details"] == {}
    assert public_key not in str(repo.audit_rows)

    rejected = await client.put(
        "/admin/agent/compute-defaults/cloudlab",
        headers=ADMIN,
        json={"ssh_public_key": "ssh-ed25519 not-a-real-key"},
    )
    assert rejected.status_code == 422
    assert "not-a-real-key" not in rejected.text
    assert repo.defaults["cloudlab"]["ssh_public_key"] == f"{public_key} researcher@lab"


@pytest.mark.asyncio
async def test_cloudlab_defaults_fill_omitted_request_values_but_explicit_values_win(compute_api):
    client, repo, _ = compute_api
    repo.defaults["cloudlab"] = {
        "project": "default-project",
        "duration": 12,
        "profile_project": "default-profile-project",
        "profile_name": "default-profile",
        "ssh_username": "default-user",
    }
    connection_id = await connection(client)
    response = await client.post(
        "/admin/agent/servers",
        headers=ADMIN,
        json={
            "name": "Experiment",
            "request_id": "defaults-request",
            "provider_id": "cloudlab",
            "mode": "provision",
            "connection_id": connection_id,
            "configuration": {"duration": 24},
            "deployment_profile": "kata",
        },
    )

    assert response.status_code == 202, response.text
    allocation = next(iter(repo.allocations.values()))
    assert allocation["configuration"] == {
        "project": "default-project",
        "duration": 24,
        "profile_project": "default-profile-project",
        "profile_name": "default-profile",
        "credential_ref": connection_id,
        "node_types": list(DEFAULT_CLOUDLAB_NODE_TYPES),
        "node_type_binding": "phystype",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_types", [None, [], ["c6620", "r6615"]])
async def test_new_cloudlab_allocations_snapshot_ordered_defaults_and_allow_overrides(
    compute_api, explicit_types
):
    client, repo, _ = compute_api
    defaults = {"node_types": ["r6525", "r650"], "node_type_binding": "hardware"}
    saved = await client.put("/admin/agent/compute-defaults/cloudlab", headers=ADMIN, json=defaults)
    assert saved.status_code == 200, saved.text
    configured = {"project": "research"}
    if explicit_types is not None:
        configured.update(node_types=explicit_types, node_type_binding="phystype")
    response = await client.post(
        "/admin/agent/servers",
        headers=ADMIN,
        json={
            "name": "Experiment",
            "request_id": "ordered-node-defaults",
            "provider_id": "cloudlab",
            "mode": "provision",
            "connection_id": await connection(client),
            "configuration": configured,
            "deployment_profile": "kata",
        },
    )
    assert response.status_code == 202, response.text
    allocation = next(iter(repo.allocations.values()))
    expected = explicit_types if explicit_types is not None else ["r6525", "r650"]
    assert allocation["configuration"]["node_types"] == expected
    assert allocation["configuration"]["node_type_binding"] == (
        "phystype" if explicit_types is not None else "hardware"
    )
    await client.put(
        "/admin/agent/compute-defaults/cloudlab",
        headers=ADMIN,
        json={"node_types": ["c6525-25g"]},
    )
    assert allocation["configuration"]["node_types"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"node_types": ["r6615", "r6615"]},
        {"node_types": ["invalid/type"]},
        {"node_types": [f"type-{i}" for i in range(21)]},
        {"node_type_binding": "invalid.parameter"},
    ],
)
async def test_cloudlab_defaults_reject_invalid_preference_lists(compute_api, invalid):
    client, repo, _ = compute_api
    response = await client.put(
        "/admin/agent/compute-defaults/cloudlab", headers=ADMIN, json=invalid
    )
    assert response.status_code == 422, response.text
    assert repo.defaults == {}


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
    # The list read itself is not an action: the Servers page polls it.
    assert [row["action"] for row in repo.audit_rows] == ["agent.compute.connection.create"]
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
    row["configuration"].update(
        extra="private-config-canary",
        host_key_source="known_hosts",
        ssh_known_hosts_ref=UNCHECKED_CLOUDLAB_HOST_KEY_REF,
    )
    row["hosts"] = [
        {"host": "host-a", "compute_node_id": "node-a", "credential_hash": "private-hash-canary"}
    ]
    response = await client.get("/admin/agent/servers", headers=ADMIN)
    assert response.status_code == 200
    public = response.json()["allocations"][0]
    assert public["connection_id"] == body["connection_id"]
    assert public["ssh_connection_id"] == "ssh-access-id"
    assert public["ssh_host_key_policy"] == "unchecked"
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
    assert UNCHECKED_CLOUDLAB_HOST_KEY_REF not in response.text
    assert "configuration" not in public
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({}, "known_hosts"),
        ({"host_key_source": "known_hosts"}, "known_hosts"),
        ({"host_key_source": "registration"}, "registration"),
    ],
)
async def test_inventory_projects_only_the_effective_cloudlab_host_key_policy(
    compute_api, configuration, expected
):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.allocations[operation["allocation_id"]]["configuration"] = configuration

    response = await client.get("/admin/agent/servers", headers=ADMIN)

    assert response.status_code == 200
    public = response.json()["allocations"][0]
    assert public["ssh_host_key_policy"] == expected
    assert "configuration" not in public


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
async def test_storage_path_is_persisted_per_server_and_replay_keeps_it(compute_api):
    client, repo, app = compute_api
    commands = app.state.compute_commands
    original_profile = commands.configuration.profiles["kata"]
    body = {
        "name": "Storage configured",
        "request_id": "storage-a",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": await connection(client),
        "configuration": {"project": "research"},
        "sandbox_storage_path": " /mnt/sandbox-data/ ",
    }
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    operation = response.json()["operation"]
    saved = repo.allocations[operation["allocation_id"]]["deployment_profile"]
    assert saved["sandbox_storage_path"] == "/mnt/sandbox-data"
    assert repo.operations[operation["id"]]["parameters"]["sandbox_storage_path"] == (
        "/mnt/sandbox-data"
    )
    assert commands.configuration.profiles["kata"] is original_profile
    assert original_profile.sandbox_storage_path is None
    inventory = await client.get("/admin/agent/servers", headers=ADMIN)
    assert inventory.status_code == 200
    assert inventory.json()["allocations"][0]["sandbox_storage_path"] == "/mnt/sandbox-data"

    commands.configuration.profiles.clear()
    replay = await client.post(
        "/admin/agent/servers",
        headers=ADMIN,
        json={**body, "sandbox_storage_path": "/mnt/sandbox-data"},
    )
    assert replay.status_code == 202
    assert replay.json()["operation"]["id"] == operation["id"]
    assert len(repo.allocations) == len(repo.operations) == 1
    conflict = await client.post(
        "/admin/agent/servers",
        headers=ADMIN,
        json={**body, "sandbox_storage_path": "/mnt/another-disk"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_storage_path_is_in_preflight_profile(compute_api):
    client, repo, _ = compute_api
    body = {
        "name": "Preflight storage",
        "request_id": "storage-preflight",
        "provider_id": "cloudlab",
        "mode": "attach",
        "connection_id": await connection(client),
        "sandbox_storage_path": "/mydata",
    }
    response = await client.post("/admin/agent/server-preflights", headers=ADMIN, json=body)
    assert response.status_code == 202, response.text
    operation = repo.operations[response.json()["operation"]["id"]]
    assert operation["parameters"]["profile"]["sandbox_storage_path"] == "/mydata"
    assert not repo.allocations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative/storage",
        "/",
        "/mnt",
        "/var/lib",
        "/etc/storage",
        "/var/lib/docker",
        "/var/lib/containerd/data",
        "/mnt/../data",
        "/mnt/./data",
        "/mnt//data",
        "/mnt/data\nother",
        "/mnt/data;command",
        "/" + "x" * 4096,
    ],
)
async def test_invalid_storage_path_is_rejected_before_any_operation(compute_api, path):
    client, repo, _ = compute_api
    body = {
        "name": "Invalid storage",
        "request_id": "storage-invalid",
        "provider_id": "cloudlab",
        "mode": "provision",
        "connection_id": await connection(client),
        "sandbox_storage_path": path,
    }
    response = await client.post("/admin/agent/servers", headers=ADMIN, json=body)
    assert response.status_code == 422, response.text
    assert not repo.allocations and not repo.operations


@pytest.mark.asyncio
async def test_omitted_storage_path_preserves_legacy_profile_mount(compute_api):
    client, repo, app = compute_api
    profiles = app.state.compute_commands.configuration.profiles
    profiles["kata"] = profiles["kata"].model_copy(update={"data_mount": "/legacy-data"})
    _, operation = await allocation(client)
    saved = repo.allocations[operation["allocation_id"]]["deployment_profile"]
    assert saved["sandbox_storage_path"] is None
    assert saved["data_mount"] == "/legacy-data"
    assert "sandbox_storage_path" not in repo.operations[operation["id"]]["parameters"]
    inventory = await client.get("/admin/agent/servers", headers=ADMIN)
    assert inventory.status_code == 200
    assert inventory.json()["allocations"][0]["sandbox_storage_path"] == "/legacy-data"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_input", [False, True])
async def test_concurrent_setup_selection_recovers_duplicate_before_capacity_refusal(
    compute_api, monkeypatch, changed_input
):
    client, repo, app = compute_api
    commands = app.state.compute_commands
    configure = commands._configuration
    allocate = repo.create_allocation_operation
    first_request_entered = asyncio.Event()
    both_requests_entered = asyncio.Event()
    first_allocated = asyncio.Event()
    entered = 0

    async def interleaved_configuration(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 1:
            first_request_entered.set()
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

    async def submit_second_request():
        # HTTP dependencies can reorder requests before they reach the command.
        # Start the duplicate only once the original is blocked in configuration.
        await first_request_entered.wait()
        return await client.post("/admin/agent/servers", headers=ADMIN, json=second_body)

    first, second = await asyncio.wait_for(
        asyncio.gather(
            client.post("/admin/agent/servers", headers=ADMIN, json=body),
            submit_second_request(),
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
async def test_definite_pre_submit_autoscale_rejection_can_be_cancelled(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    allocation_row = repo.allocations[operation["allocation_id"]]
    allocation_row["autoscaling_owned"] = True
    repo.operations[operation["id"]].update(
        state="blocked",
        step="pending",
        result={"external_pending": False},
    )

    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)

    assert observed.status_code == 200
    assert observed.json()["operation"]["allowed_actions"] == ["retry", "cancel"]
    cancelled = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/cancel",
        headers=ADMIN,
        json={},
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["operation"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_terminal_autoscale_acquisition_cannot_be_retried(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    allocation_row = repo.allocations[operation["allocation_id"]]
    allocation_row.update(
        autoscaling_owned=True,
        autoscaling_member_state="released",
        observed_state="terminated",
    )
    repo.operations[operation["id"]].update(
        state="failed",
        result={"external_pending": False},
    )

    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)

    assert observed.status_code == 200
    assert observed.json()["operation"]["allowed_actions"] == []
    retried = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/retry",
        headers=ADMIN,
        json={},
    )
    assert retried.status_code == 409


@pytest.mark.asyncio
async def test_released_allocation_extension_cannot_be_retried(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.allocations[operation["allocation_id"]]["release_committed_at"] = datetime.now(UTC)
    repo.operations[operation["id"]].update(kind="extend", state="blocked")

    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)

    assert observed.status_code == 200
    assert observed.json()["operation"]["allowed_actions"] == []
    retried = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/retry",
        headers=ADMIN,
        json={},
    )
    assert retried.status_code == 409


@pytest.mark.asyncio
async def test_released_unknown_acquisition_cannot_be_resolved(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.allocations[operation["allocation_id"]]["release_committed_at"] = datetime.now(UTC)
    repo.operations[operation["id"]].update(state="unknown", result={})

    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)
    assert observed.json()["operation"]["allowed_actions"] == []
    resolved = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/resolve",
        headers=ADMIN,
        json={"external_id": "already-released"},
    )
    assert resolved.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["retire", "discard"])
async def test_terminal_autoscale_lifecycle_advertises_neither_retry_nor_cancel(compute_api, kind):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    allocation_row = repo.allocations[operation["allocation_id"]]
    allocation_row.update(
        autoscaling_owned=True,
        autoscaling_member_state="released",
        observed_state="terminated",
        release_committed_at=datetime.now(UTC),
    )
    repo.operations[operation["id"]].update(
        kind=kind,
        state="blocked",
        result={"external_pending": False},
    )

    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)

    assert observed.status_code == 200
    assert observed.json()["operation"]["allowed_actions"] == []
    for action in ("retry", "cancel"):
        refused = await client.post(
            f"/admin/agent/compute-operations/{operation['id']}/{action}",
            headers=ADMIN,
            json={},
        )
        assert refused.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "result", "external_id"),
    [
        ("unknown", {"external_pending": False}, None),
        ("blocked", {"external_pending": True}, None),
        (
            "blocked",
            {
                "external_pending": False,
                "resource_ref": {"provider_id": "cloudlab", "external_id": "experiment-a"},
            },
            None,
        ),
        ("blocked", {"external_pending": False}, "experiment-a"),
    ],
    ids=["unknown", "external-pending", "receipt-identity", "allocation-identity"],
)
async def test_unsafe_autoscale_acquisition_does_not_advertise_cancel(
    compute_api, state, result, external_id
):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    allocation_row = repo.allocations[operation["allocation_id"]]
    allocation_row.update(autoscaling_owned=True, external_id=external_id)
    repo.operations[operation["id"]].update(
        state=state,
        step="pending",
        result=result,
    )

    observed = await client.get(f"/admin/agent/compute-operations/{operation['id']}", headers=ADMIN)

    assert observed.status_code == 200
    assert "cancel" not in observed.json()["operation"]["allowed_actions"]
    cancelled = await client.post(
        f"/admin/agent/compute-operations/{operation['id']}/cancel",
        headers=ADMIN,
        json={},
    )
    assert cancelled.status_code == 409
    assert repo.operations[operation["id"]]["state"] == state


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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["terminated", "absent"])
async def test_inventory_omits_stale_observation_warning_for_terminal_resources(compute_api, state):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.allocations[operation["allocation_id"]].update(
        observed_state=state,
        observation_error="credentials_expired",
    )

    response = await client.get("/admin/agent/servers", headers=ADMIN)

    assert response.status_code == 200
    reasons = response.json()["allocations"][0]["blocked_reasons"]
    assert not any(reason["code"] == "observation_stale" for reason in reasons)


@pytest.mark.asyncio
async def test_inventory_keeps_stale_observation_warning_until_release_is_confirmed(compute_api):
    client, repo, _ = compute_api
    _, operation = await allocation(client)
    repo.allocations[operation["allocation_id"]].update(
        observed_state="unknown",
        observation_error="credentials_expired",
        release_committed_at=datetime.now(UTC),
    )

    response = await client.get("/admin/agent/servers", headers=ADMIN)

    assert response.status_code == 200
    reasons = response.json()["allocations"][0]["blocked_reasons"]
    assert [reason["code"] for reason in reasons] == ["observation_stale"]


def deployment_bundle() -> str:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in (
            "deploy/host/sandbox_host.sh",
            "deploy/host/setup_kata_runtime.sh",
            "deploy/host/docker-compose.sandbox.yml",
            "pyproject.toml",
        ):
            data = b"fixture"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return base64.b64encode(output.getvalue()).decode()


@pytest.mark.asyncio
async def test_admin_profile_inputs_are_encrypted_and_never_returned(compute_api):
    client, repo, _ = compute_api
    response = await client.post(
        "/admin/agent/compute-deployment-profiles",
        headers=ADMIN,
        json={
            "id": "admin-kata",
            "label": "Admin Kata",
            "deployment_sha": "c" * 40,
            "deploy_root": "/opt/cloud-agent-admin",
            "environment": "AGENT_SANDBOX_IMAGE=image",
            "files": {
                ".compute-secrets/gateway.key": "private-key-canary",
                ".compute-secrets/gateway.known_hosts": "known-host-canary",
            },
            "source_bundle_base64": deployment_bundle(),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"profile": {"id": "admin-kata", "label": "Admin Kata"}}
    assert "private-key-canary" not in response.text
    stored = repo.profiles["admin-kata"]
    assert "private-key-canary" not in stored["secret_ciphertext"]
    assert "private-key-canary" not in str(stored["configuration"])

    catalog = await client.get("/admin/agent/compute-providers", headers=ADMIN)
    assert {"id": "admin-kata", "label": "Admin Kata"} in catalog.json()["deployment_profiles"]
    assert "private-key-canary" not in catalog.text
