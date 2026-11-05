"""Admin deployment profile inputs are stored exactly as submitted, within their limits."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.servers.test_admin_compute import ADMIN, compute_api as compute_api, deployment_bundle

from cloud_agent.compute_secrets import StoredDeploymentResolver

if TYPE_CHECKING:
    from fastapi import FastAPI


def ephemeral_openssh_private_key() -> str:
    """A throwaway key generated per test; never print or log its text."""
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )


def profile_body(**changes: Any) -> dict[str, Any]:
    return {
        "id": "byte-exact",
        "label": "Byte exact",
        "deployment_sha": "c" * 40,
        "environment": "AGENT_SANDBOX_IMAGE=image\n",
        "files": {".compute-secrets/gateway.known_hosts": "known-host-canary\n"},
        "source_bundle_base64": deployment_bundle(),
        **changes,
    }


def fingerprint(text: str) -> tuple[int, str]:
    """Compare exact bytes without letting a failure print credential text."""
    data = text.encode()
    return len(data), hashlib.sha256(data).hexdigest()


async def stored_inputs(app: FastAPI, profile_id: str) -> tuple[str, dict[str, str]]:
    """Read back through the resolver the Host deployment uses."""
    commands = app.state.compute_commands
    resolver = StoredDeploymentResolver(commands.repository, commands.secrets)
    row = await commands.repository.get_deployment_profile(profile_id)
    environment = await resolver.resolve(f"stored-profile:{profile_id}:environment")
    files = {
        name: await resolver.resolve(f"stored-profile:{profile_id}:file:{name}")
        for name in row["configuration"]["files"]
    }
    return environment, files


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment",
    [
        "ENV=value\n",
        "AGENT_SANDBOX_IMAGE=image\r\nAGENT_RELAY_URL=https://relay.example\r\n",
        "  AGENT_SANDBOX_IMAGE=image\n\n",
        "\tAGENT_SANDBOX_IMAGE=image  ",
    ],
)
async def test_admin_profile_inputs_are_stored_byte_for_byte(compute_api, environment):
    client, _, app = compute_api
    private_key = ephemeral_openssh_private_key()
    assert private_key.endswith("-----END OPENSSH PRIVATE KEY-----\n")
    files = {
        ".compute-secrets/gateway.key": private_key,
        ".compute-secrets/gateway.known_hosts": "host ssh-ed25519 AAAA\r\nother ssh-ed25519 BBBB\r\n",
        ".compute-secrets/leading": "  synthetic file\n",
        ".compute-secrets/boundary": "x" * 65535 + "\n",
    }

    response = await client.post(
        "/admin/agent/compute-deployment-profiles",
        headers=ADMIN,
        json=profile_body(environment=environment, files=files),
    )

    assert response.status_code == 201, response.text
    stored_environment, stored_files = await stored_inputs(app, "byte-exact")
    assert fingerprint(stored_environment) == fingerprint(environment)
    assert stored_files.keys() == files.keys()
    for name, content in files.items():
        assert fingerprint(stored_files[name]) == fingerprint(content), name


@pytest.mark.asyncio
async def test_admin_profile_identifiers_and_labels_are_still_trimmed(compute_api):
    client, repo, app = compute_api
    response = await client.post(
        "/admin/agent/compute-deployment-profiles",
        headers=ADMIN,
        json=profile_body(
            id="  byte-exact\n",
            label="  Byte exact  ",
            deployment_sha=" " + "c" * 40 + "\n",
            deploy_root=" /opt/cloud-agent-exact ",
            files={" .compute-secrets/gateway.key\n": "  synthetic file\n"},
        ),
    )

    assert response.status_code == 201, response.text
    assert response.json() == {"profile": {"id": "byte-exact", "label": "Byte exact"}}
    stored = repo.profiles["byte-exact"]
    assert stored["configuration"]["deployment_sha"] == "c" * 40
    assert stored["configuration"]["deploy_root"] == "/opt/cloud-agent-exact"
    assert stored["configuration"]["files"] == [".compute-secrets/gateway.key"]
    _, files = await stored_inputs(app, "byte-exact")
    assert files == {".compute-secrets/gateway.key": "  synthetic file\n"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"environment": ""},
        {"environment": " \r\n\t"},
        {"environment": "x" * 65537},
        {"environment": "x" * 65536 + "\n"},
        {"environment": 5},
        {"files": {}},
        {"files": {".compute-secrets/key": ""}},
        {"files": {".compute-secrets/key": "\n"}},
        {"files": {".compute-secrets/key": "x" * 65537}},
        {"files": {".compute-secrets/key": "x" * 65536 + "\n"}},
        {"files": {".compute-secrets/key": 5}},
        {"files": {f".compute-secrets/{index}": "x" for index in range(25)}},
        {"files": {"../outside": "x\n"}},
        {"files": {".compute-secrets/../outside": "x\n"}},
        {"id": "Bad id"},
        {"label": "  "},
        {"deployment_sha": "c" * 39},
        {"deploy_root": "/opt/cloud agent"},
    ],
)
async def test_admin_profile_inputs_keep_their_limits(compute_api, changes):
    client, repo, _ = compute_api
    response = await client.post(
        "/admin/agent/compute-deployment-profiles", headers=ADMIN, json=profile_body(**changes)
    )

    assert response.status_code == 422, response.text
    assert repo.profiles == {}
