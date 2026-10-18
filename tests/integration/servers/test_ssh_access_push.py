"""An SSH change made while a group starts reaches that turn, over the real store and routes.

A claim carries its group's SSH state as it was when the turn was claimed, and
an owner's change is pushed only to groups that are running. A group that is
new, stopped or cold is reserved after the claim and stays ``starting`` until
the runner confirms its container, so a change made in between reached no
group, and the runner then applied the claim's older state as the newest.

No sandbox manager runs in this tier: the one the host advertises is a
recorder, so these cases see exactly which states the control plane sent where.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from tests.integration.servers.test_pending_message_queue import (
    _HOST,
    _IMAGE_DIGEST,
    _OWNER,
    _claim_body,
    api as api,
    pool as pool,
)
from tests.support.ssh_keys import host_key, private_key

from cloud_agent.api import ssh_servers
from cloud_agent.application.ssh_servers import ssh_key_cipher
from cloud_agent.sandbox_manager_client import SandboxManagerClient
from cloud_agent_shared.ssh_access import SSH_ACCESS_CAPABILITY, SshAccess

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _ready_server(api) -> str:
    """One server its owner checked, holding a real encrypted key."""
    server_id = str(uuid.uuid4())
    row = await api.store.ssh_servers.create_ssh_server(
        user_id=_OWNER,
        values={
            "id": server_id,
            "name": "gpu",
            "description": "",
            "address": "gpu.example.org",
            "port": 22,
            "username": "ubuntu",
            "public_key": "ssh-ed25519 AAAA cloud-agent:gpu",
            "private_key_ciphertext": ssh_key_cipher().encrypt(private_key()),
        },
    )
    await api.store.ssh_servers.update_ssh_server(
        user_id=_OWNER,
        server_id=server_id,
        revision=row["revision"],
        values={
            "status": "ready",
            "pinned_ipv4": "93.184.216.34",
            "host_key_type": "ssh-ed25519",
            "host_key": host_key(),
        },
        event="verified",
    )
    return server_id


async def test_a_change_made_while_the_group_starts_reaches_the_starting_turn(api, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "ssh-push-test-key")
    api.app.include_router(ssh_servers.router)
    await api.store.host_pool.record_host_heartbeat(
        host=_HOST, sandbox_manager_url="http://sandbox-manager.test:9000"
    )
    sent: list[tuple[str, SshAccess]] = []

    async def sync_ssh_access(_client: Any, workspace_id: str, access: SshAccess) -> dict:
        sent.append((workspace_id, access))
        return {"newest": True, "revision": access.revision, "running": True}

    monkeypatch.setattr(SandboxManagerClient, "sync_ssh_access", sync_ssh_access)
    server_id = await _ready_server(api)
    job = await api.create(ssh_server_ids=[server_id])
    body = _claim_body("w")
    body["attestation"]["runner_capabilities"].append(SSH_ACCESS_CAPABILITY)
    response = await api.client.post("/v1/agent/worker/claim", json=body)
    assert response.status_code == 200, response.text
    claim = response.json()
    assert claim["job_id"] == job["id"]
    claimed = SshAccess.model_validate(claim["ssh_access"])
    assert [server.name for server in claimed.members[0].servers] == ["gpu"]
    thread_id = claim["thread_id"]
    async with api.pool.acquire() as conn:
        lease_generation = await conn.fetchval(
            "SELECT lease_generation FROM agent_attempts WHERE id = $1", claim["attempt_id"]
        )
    # The runner reserves the new group, which starts.
    reserved = await api.store.sandbox.reserve_thread_sandbox(
        thread_id=thread_id,
        attempt_id=claim["attempt_id"],
        lease_generation=lease_generation,
        host=_HOST,
        sandbox_id=f"asbx_{thread_id}",
        container_name=f"chat-{thread_id}",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=_IMAGE_DIGEST,
        execution_profile=claim["execution_profile"],
    )
    assert reserved is not None and reserved["state"] == "starting"

    # The owner turns gpu off while the group starts. No group of the chat is
    # running yet, so the change's own push reaches nothing.
    response = await api.client.put(
        f"/v1/agent/ssh-servers/threads/{thread_id}/bindings", json={"server_ids": []}
    )
    assert response.status_code == 200, response.text
    assert sent == []

    # The runner confirms the container. From here every change finds the
    # group, and the push sent now carries the ones made before.
    response = await api.client.post(
        f"/v1/agent/worker/jobs/{job['id']}/sandbox/running",
        json={
            "sandbox_generation": reserved["generation"],
            "activation_generation": reserved["activation_generation"],
            "container_id": f"container-{thread_id}",
            "context_status": "new",
        },
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert response.status_code == 200, response.text

    [(workspace_id, pushed)] = sent
    assert workspace_id == thread_id
    # Newer than the claim's state, which the runner applies next: the sandbox
    # manager keeps the newest, so the claim's gpu is narrowed away and its
    # bridge keeps no exception for it.
    assert pushed.revision > claimed.revision
    assert pushed.members == []
    assert pushed.targets() == set()
