"""Compute credentials are bound, write-only, and unavailable without a key."""

import json
from typing import Any

import pytest

from cloud_agent.compute_secrets import (
    ComputeSecretError,
    ComputeSecretStore,
    StoredDeploymentResolver,
)
from cloud_agent.ports.external.compute_provider import (
    UNCHECKED_CLOUDLAB_HOST_KEY_REF,
)


class Connections:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        return self.rows.get(connection_id)

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        return self.profiles.get(profile_id)


def test_cipher_refuses_plaintext_fallback_and_short_keys() -> None:
    empty = ComputeSecretStore(Connections(), key="")
    assert not empty.configured
    with pytest.raises(ComputeSecretError, match="not configured"):
        empty.encrypt("connection", "cloudlab", "token")
    with pytest.raises(ComputeSecretError, match="32 bytes"):
        ComputeSecretStore(Connections(), key="short")


def test_cipher_binds_provider_connection_and_key() -> None:
    cipher = ComputeSecretStore(Connections(), key="k" * 32)
    ciphertext = cipher.encrypt("one", "cloudlab", "secret-value")
    assert "secret-value" not in ciphertext
    assert cipher.decrypt("one", "cloudlab", ciphertext) == "secret-value"
    for connection_id, provider_id in [("two", "cloudlab"), ("one", "existing_ssh")]:
        with pytest.raises(ComputeSecretError, match="cannot be decrypted"):
            cipher.decrypt(connection_id, provider_id, ciphertext)
    with pytest.raises(ComputeSecretError, match="cannot be decrypted"):
        ComputeSecretStore(Connections(), key="z" * 32).decrypt("one", "cloudlab", ciphertext)


@pytest.mark.asyncio
async def test_resolution_separates_private_credentials_from_verified_host_keys() -> None:
    connections = Connections()
    secrets = ComputeSecretStore(connections, key="k" * 32)
    connections.rows["ssh"] = {
        "provider_id": "existing_ssh",
        "configuration": {"known_hosts": "host ssh-ed25519 public-key"},
        "secret_ciphertext": secrets.encrypt("ssh", "existing_ssh", "private-key"),
    }
    assert await secrets.resolve("ssh") == "private-key"
    assert await secrets.resolve("ssh:known_hosts") == "host ssh-ed25519 public-key"
    with pytest.raises(ComputeSecretError, match="unsupported"):
        await secrets.resolve("ssh:configuration")
    connections.rows["ssh"]["configuration"] = {}
    with pytest.raises(ComputeSecretError, match="unsupported"):
        await secrets.resolve(UNCHECKED_CLOUDLAB_HOST_KEY_REF)
    with pytest.raises(ComputeSecretError, match="verified SSH"):
        await secrets.resolve("ssh:known_hosts")
    with pytest.raises(ComputeSecretError, match="no longer exists"):
        await secrets.resolve("missing")


@pytest.mark.asyncio
async def test_stored_profile_resolver_decrypts_assets_and_returns_bundle() -> None:
    connections = Connections()
    secrets = ComputeSecretStore(connections, key="k" * 32)
    plaintext = json.dumps(
        {
            "environment": "AGENT_SANDBOX_IMAGE=image",
            "files": {".compute-secrets/key": "secret"},
        }
    )
    connections.profiles["profile"] = {
        "source_bundle": b"bundle",
        "secret_ciphertext": secrets.encrypt("profile:profile", "deployment_profile", plaintext),
    }
    resolver = StoredDeploymentResolver(connections, secrets)

    assert await resolver.bundle("stored-profile:profile:bundle") == b"bundle"
    assert (
        await resolver.resolve("stored-profile:profile:environment") == "AGENT_SANDBOX_IMAGE=image"
    )
    assert await resolver.resolve("stored-profile:profile:file:.compute-secrets/key") == "secret"
    with pytest.raises(ComputeSecretError, match="credential"):
        await resolver.resolve("stored-profile:profile:file:missing")
