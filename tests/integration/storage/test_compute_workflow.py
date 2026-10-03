"""Exercise the actual durable worker against Postgres with inert external ports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.integration.storage.test_host_pool_store import db as db, hosts as hosts, store as store

from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.compute_registry import ComputeProviderRegistration, ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.host_pool import hash_secret
from cloud_agent.ports.external.compute_provider import (
    NodeObservation,
    ProviderCapabilities,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
    SshConnection,
)
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapResult,
    DeploymentProfile,
    HostInventory,
)
from cloud_agent.storage._sql.compute_admission import host_compute_admission
from cloud_agent.workers.compute_resources import ComputeResourceWorker

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def rig(store, hosts, monkeypatch):
    """Keep external effects observable while using real persistence and enrollment."""
    resource_id = str(uuid4())
    profile = DeploymentProfile(
        id="test",
        label="Test",
        source_bundle="/test/source.tar",
        source_sha256="a" * 64,
        deployment_sha="b" * 40,
        environment_ref="deployment:env",
    )
    ref = ResourceRef(external_id=resource_id, config={"external_id": resource_id})
    node = NodeObservation(
        provider_node_id="node-1",
        instance_identity=resource_id + ":node-1",
        display_name="lab-node",
        connection=SshConnection(
            address="192.0.2.10",
            username="test",
            credential_ref="test",
            known_hosts_ref="test:known_hosts",
        ),
    )
    observation = ResourceObservation(
        ref=ref,
        state="ready",
        raw_state="ready",
        observed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        lease_known=True,
        nodes=(node,),
        scope_complete=True,
    )
    provider = SimpleNamespace(
        capabilities=lambda: ProviderCapabilities(
            acquisition_modes=("attach", "provision"), can_extend=True, can_release=True
        ),
        acquire=AsyncMock(
            return_value=ProviderReceipt(outcome="confirmed", resource=ref, observation=observation)
        ),
        inspect=AsyncMock(return_value=observation),
        recover_acquire=AsyncMock(),
        release=AsyncMock(),
    )
    registry = ComputeProviderRegistry(
        [ComputeProviderRegistration(id="fixture", label="Fixture", provider=provider, fields=())]
    )
    commands = ComputeCommands(
        repository=store.compute_resources,
        registry=registry,
        secrets=ComputeSecretStore(store.compute_resources, key="x" * 32),
        configuration=ComputeConfiguration(profiles={profile.id: profile}),
    )
    bootstrap = SimpleNamespace(inventory=AsyncMock(return_value=HostInventory(complete=True)))

    async def deploy(node, profile, intent, token):
        assert token is not None
        await hosts.register_host(
            host=intent.host, credential_hash=hash_secret("host-credential"), slots_total=2
        )
        await hosts.record_host_heartbeat(host=intent.host)
        return BootstrapResult(ready=True, host=intent.host, deployment_sha=profile.deployment_sha)

    async def readiness(node, profile, intent):
        return BootstrapResult(ready=True, host=intent.host, deployment_sha=profile.deployment_sha)

    bootstrap.deploy = AsyncMock(side_effect=deploy)
    bootstrap.readiness = AsyncMock(side_effect=readiness)
    broker = SimpleNamespace(
        health=AsyncMock(return_value={}), list_sandboxes=AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "cloud_agent.application.compute_reconcile.broker_for_host", lambda host: broker
    )
    reconciler = ComputeReconciler(commands=commands, host_pool=hosts, bootstrap=bootstrap)
    request_id = str(uuid4())
    op = await store.compute_resources.create_allocation_operation(
        request_id=request_id,
        fingerprint=request_id,
        kind="acquire",
        requested_by="admin",
        parameters={},
        allocation={
            "name": "test",
            "provider_id": "fixture",
            "mode": "provision",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
            "deployment_profile": profile.model_dump(mode="json"),
        },
    )
    return SimpleNamespace(
        repo=store.compute_resources,
        worker=ComputeResourceWorker(reconciler),
        commands=commands,
        op=op,
        provider=provider,
        bootstrap=bootstrap,
        broker=broker,
        observation=observation,
    )


async def run_next(rig):
    claimed = await rig.repo.claim_operation(worker_id="workflow")
    assert claimed is not None
    await rig.worker.execute_claimed(claimed)
    return await rig.repo.get_operation(claimed["id"])


async def test_acquisition_to_global_placement_then_release_requires_empty_broker(
    db, store, hosts, monkeypatch
):
    r = await rig(store, hosts, monkeypatch)
    completed = await run_next(r)
    assert completed["state"] == "succeeded", completed
    allocation = await r.repo.get_allocation(r.op["allocation_id"])
    host = allocation["hosts"][0]["host"]
    assert (await hosts.get_host(host=host))["enrolled"]
    async with db.acquire() as conn:
        admitted, reasons = await host_compute_admission(conn, host)
    assert admitted, reasons
    assert await hosts.set_active_runner_host(host=host)
    assert await hosts.set_active_runner_host(host=None)

    async def release(ref, intent):
        assert ref.external_id == r.observation.ref.external_id
        gone = r.observation.model_copy(
            update={"state": "terminated", "raw_state": "terminated", "nodes": ()}
        )
        return ProviderReceipt(outcome="confirmed", resource=ref, observation=gone)

    r.provider.release.side_effect = release
    r.broker.list_sandboxes.return_value = [{"sandbox_id": "stopped-group", "status": "stopped"}]
    await r.repo.create_operation(
        request_id="retire",
        fingerprint="retire",
        kind="retire",
        allocation_id=allocation["id"],
        parameters={},
        requested_by="admin",
    )
    blocked = await run_next(r)
    assert blocked["state"] == "blocked", blocked
    r.provider.release.assert_not_awaited()
    assert (await hosts.get_host(host=host))["enrolled"]
    r.broker.list_sandboxes.return_value = []
    await r.repo.retry_operation(blocked["id"])
    retired = await run_next(r)
    assert retired["state"] == "succeeded", retired
    assert (await r.repo.get_allocation(allocation["id"]))["release_committed_at"]
    assert not (await hosts.get_host(host=host))["enrolled"]
    r.provider.release.assert_awaited_once()


async def test_receipt_survives_failed_observation_and_retry_never_creates_twice(
    db, store, hosts, monkeypatch
):
    from cloud_agent.ports.external.compute_provider import ProviderError

    r = await rig(store, hosts, monkeypatch)
    r.provider.acquire.return_value = ProviderReceipt(outcome="pending", resource=r.observation.ref)
    r.provider.inspect.side_effect = ProviderError(
        "temporarily_unavailable", "unavailable", retryable=True
    )
    waiting = await run_next(r)
    assert waiting["state"] == "waiting"
    assert waiting["result"]["resource_ref"]["external_id"] == r.observation.ref.external_id
    r.provider.inspect.side_effect = None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET next_attempt_at=NOW() WHERE id=$1", waiting["id"]
        )
    completed = await run_next(r)
    assert completed["state"] == "succeeded", completed
    r.provider.acquire.assert_awaited_once()
    r.provider.recover_acquire.assert_not_awaited()
