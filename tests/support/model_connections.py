"""Owner model connections for database tests, stored the way the API stores them.

The key is encrypted under the deployment's real connection cipher and each
model carries a recorded verification, so admission, claims and residencies
resolve it exactly as they resolve one an owner saved and verified. Only the
provider is absent: nothing here dials it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.application.model_connections import model_connection_cipher, new_connection_id
from cloud_agent_shared.model_connections import MODEL_CONNECTION_CAPABILITY, PROTOCOLS

if TYPE_CHECKING:
    import pytest

    from cloud_agent.storage.assembly import Storage

#: The deployment key the helpers encrypt under; set it with :func:`use_test_cipher`.
TEST_ENCRYPTION_KEY = "model-connection-test-encryption-key"
#: A provider key that is plainly a test value.
TEST_API_KEY = "sk-test-personal-provider-key-0001"
#: Every native API supported: each Agent has a path to the model.
ALL_SUPPORTED = {"protocols": {protocol: {"status": "supported"} for protocol in PROTOCOLS}}


def use_test_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the connection cipher usable in this test."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)


def with_connection_capability(body: dict[str, Any]) -> dict[str, Any]:
    """A claim body whose host can launch a member on an owner connection."""
    attestation = dict(body["attestation"])
    capabilities = list(attestation["runner_capabilities"])
    if MODEL_CONNECTION_CAPABILITY not in capabilities:
        capabilities.append(MODEL_CONNECTION_CAPABILITY)
    attestation["runner_capabilities"] = capabilities
    return {**body, "attestation": attestation}


async def verified_connection(
    store: Storage,
    user_id: str,
    *,
    name: str = "My provider",
    models: tuple[str, ...] = ("provider-model",),
    result: dict[str, Any] | None = None,
    api_key: str = TEST_API_KEY,
    connection_id: str | None = None,
) -> dict[str, Any]:
    """Create ``user_id``'s connection with every model's verification atomically.

    ``result`` defaults to every API supported. Like the verified-save API,
    the encrypted key and every listed model's facts enter the same insert.
    """
    checks = dict.fromkeys(models, result or ALL_SUPPORTED)
    row = await store.model_connections.create_model_connection(
        user_id=user_id,
        values={
            "id": connection_id or new_connection_id(),
            "name": name,
            "base_url": "https://provider.example/v1",
            "models": list(models),
            "api_key_ciphertext": model_connection_cipher().encrypt(api_key),
            "model_checks": checks,
        },
    )
    current = await store.model_connections.get_model_connection(
        user_id=user_id, connection_id=row["id"]
    )
    assert current is not None
    assert current["model_checks"] == {
        model: {**facts, "revision": current["config_revision"]} for model, facts in checks.items()
    }
    return current


__all__ = [
    "ALL_SUPPORTED",
    "TEST_API_KEY",
    "TEST_ENCRYPTION_KEY",
    "use_test_cipher",
    "verified_connection",
    "with_connection_capability",
]
