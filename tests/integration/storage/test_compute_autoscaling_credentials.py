"""Credential rotation stays fenced through an autoscaled provider submission."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from tests.integration.storage.test_host_pool_store import db as db, store as store

from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.ports.external.compute_provider import (
    AcquireRequest,
    NodeRegistration,
    OperationIntent,
)
from cloud_agent.providers.compute.cloudlab import CloudLabProvider

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class _BarrierResolver:
    """Stop after the repository check but before resolving the provider token."""

    def __init__(self, secrets: ComputeSecretStore) -> None:
        self.secrets = secrets
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, reference: str) -> str:
        self.entered.set()
        await self.release.wait()
        return await self.secrets.resolve(reference)


async def test_rotation_cannot_cross_autoscale_assertion_to_provider_resolution(db, store):
    """A paused policy still cannot rotate credentials while its POST is in flight."""
    repo = store.compute_resources
    secrets = ComputeSecretStore(repo, key="credential-fence-test-key-material")
    cloudlab_id = "race-cloudlab"
    ssh_id = "race-ssh"
    old_token = "verified-token-v1"
    new_token = "unverified-token-v2"
    for connection_id, provider_id, secret in (
        (cloudlab_id, "cloudlab", old_token),
        (ssh_id, "existing_ssh", "ssh-private-key"),
    ):
        await repo.save_connection(
            connection_id=connection_id,
            name=connection_id,
            provider_id=provider_id,
            configuration={},
            secret_ciphertext=secrets.encrypt(connection_id, provider_id, secret),
            masked_summary="configured",
            status="verified",
        )
    policy = {
        "enabled": True,
        "min_hosts": 0,
        "max_hosts": 1,
        "connection_id": cloudlab_id,
        "ssh_connection_id": ssh_id,
        "configuration": {
            "project": "research",
            "profile_name": "cloud-agent-node",
            "profile_project": "research",
            "duration": 4,
            "ssh_username": "ubuntu",
        },
        "deployment_profiles": ["race-route"],
        "sandbox_storage_path": None,
        "renewal_policy": {},
        "slot_policy": {},
        "scale_up_sustain_seconds": 120,
        "scale_down_idle_seconds": 3600,
        "cooldown_seconds": 600,
        "scale_down_enabled": False,
    }
    await repo.update_cloudlab_autoscaling(
        expected_revision=0,
        policy=policy,
        profile_snapshots={"race-route": {"id": "race-route"}},
        requested_by="admin",
    )
    queued = await repo.create_autoscale_allocation_operation(
        expected_revision=1,
        expected_generation=1,
        request_id="credential-race",
        fingerprint="credential-race",
        allocation={"name": "credential race"},
        parameters={},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_up"},
        cooldown_seconds=60,
    )
    claimed = await repo.claim_operation(worker_id="worker")
    assert claimed is not None and claimed["id"] == queued["operation"]["id"]
    allocation = await repo.get_allocation(claimed["allocation_id"])
    assert allocation is not None

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return httpx.Response(
                200,
                json={"profiles": [{"name": "cloud-agent-node", "project": "research"}]},
            )
        return httpx.Response(200, json={"id": str(uuid4())})

    resolver = _BarrierResolver(secrets)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = CloudLabProvider(
        api_url="https://cloudlab-api.example.org/v1",
        resolver=resolver,
        client=client,
    )
    request = AcquireRequest(
        mode="provision",
        config=allocation["configuration"],
        registration=NodeRegistration(
            url="https://agent.example.org/v1/compute/node-registrations",
            token=SecretStr("one-time-registration-token"),
        ),
    )
    operation_intent = OperationIntent(
        operation_id=claimed["id"],
        allocation_id=claimed["allocation_id"],
        requested_at=claimed["created_at"],
        request_fingerprint=claimed["fingerprint"],
        acquisition=request,
    )

    async def submit():
        await repo.assert_autoscale_acquire_connections_verified(
            allocation_id=claimed["allocation_id"],
            operation_id=claimed["id"],
            generation=claimed["generation"],
            worker_id="worker",
        )
        return await provider.acquire(request, operation_intent)

    submission = asyncio.create_task(submit())
    await asyncio.wait_for(resolver.entered.wait(), timeout=2)
    try:
        # Pausing removes the policy-level rotation guard. The running owned
        # acquisition remains the second, narrower guard through token resolve
        # and POST, so new unverified material cannot enter this submission.
        await repo.update_cloudlab_autoscaling(
            expected_revision=1,
            policy={**policy, "enabled": False},
            profile_snapshots={},
            requested_by="admin",
        )
        with pytest.raises(ValueError, match="autoscaling_connection_in_use"):
            await repo.save_connection(
                connection_id=cloudlab_id,
                name=cloudlab_id,
                provider_id="cloudlab",
                configuration={},
                secret_ciphertext=secrets.encrypt(cloudlab_id, "cloudlab", new_token),
                masked_summary="configured",
                status="unverified",
                expected_revision=1,
            )
        assert requests == []
    finally:
        resolver.release.set()
    receipt = await asyncio.wait_for(submission, timeout=2)
    await client.aclose()

    assert receipt.outcome == "pending"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert all(request.headers["X-Api-Token"] == old_token for request in requests)
    assert new_token not in str(requests[-1].headers)
