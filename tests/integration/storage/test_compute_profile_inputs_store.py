"""Administrator profile inputs survive the real API model, cipher and Postgres row exactly."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.integration.storage.test_host_pool_store import db as db, store as store

from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.compute_registry import ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore, StoredDeploymentResolver
from cloud_agent.schemas_compute import AddDeploymentProfile

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _bundle() -> str:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in (
            "deploy/host/sandbox_host.sh",
            "deploy/host/setup_kata_runtime.sh",
            "deploy/host/docker-compose.sandbox.yml",
            "pyproject.toml",
        ):
            member = tarfile.TarInfo(name)
            member.size = len(b"fixture")
            archive.addfile(member, io.BytesIO(b"fixture"))
    return base64.b64encode(output.getvalue()).decode()


def _fingerprint(text: str) -> tuple[int, str]:
    """Compare exact bytes without letting a failure print credential text."""
    data = text.encode()
    return len(data), hashlib.sha256(data).hexdigest()


async def test_created_profile_reads_back_byte_identical_inputs(store):
    """A key's final newline and CRLF or padded files are not rewritten on the way in."""
    repo = store.compute_resources
    secrets = ComputeSecretStore(repo, key="x" * 32)
    commands = ComputeCommands(
        repository=repo,
        registry=ComputeProviderRegistry([]),
        secrets=secrets,
        configuration=ComputeConfiguration(),
    )
    # Generated per run and never printed.
    private_key = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )
    environment = "  AGENT_SANDBOX_IMAGE=image\r\nENV=value\n"
    files = {
        ".compute-secrets/gateway.key": private_key,
        ".compute-secrets/gateway.known_hosts": "host ssh-ed25519 AAAA\r\n",
        ".compute-secrets/leading": "  synthetic file\n",
    }
    # Validated from JSON text, as the Admin request body arrives.
    request = AddDeploymentProfile.model_validate_json(
        json.dumps(
            {
                "id": "byte-exact",
                "label": " Byte exact ",
                "deployment_sha": "d" * 40,
                "environment": environment,
                "files": files,
                "source_bundle_base64": _bundle(),
            }
        )
    )

    assert await commands.create_deployment_profile(request) == {
        "id": "byte-exact",
        "label": "Byte exact",
    }

    row = await repo.get_deployment_profile("byte-exact")
    assert row is not None
    assert row["configuration"]["files"] == list(files)
    resolver = StoredDeploymentResolver(repo, secrets)
    stored = await resolver.resolve("stored-profile:byte-exact:environment")
    assert _fingerprint(stored) == _fingerprint(environment)
    for name, content in files.items():
        stored = await resolver.resolve(f"stored-profile:byte-exact:file:{name}")
        assert _fingerprint(stored) == _fingerprint(content), name
