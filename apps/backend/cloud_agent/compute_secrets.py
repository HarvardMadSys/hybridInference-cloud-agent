"""Encrypted, write-only compute credentials resolved inside the control plane.

The encryption key is separate from gateway, host and sandbox credentials.
Ciphertexts are bound to both connection identity and provider, so swapping
database values cannot turn one administrator connection into another.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

from cloud_agent.settings import setting_env

ENV_COMPUTE_SECRET_KEY = "AGENT_COMPUTE_SECRET_KEY"
_DOMAIN = b"cloud-agent/compute-credentials/v1"


class ComputeSecretError(Exception):
    """A safe error whose message contains no credential or ciphertext."""


class ComputeConnectionReader(Protocol):
    """Only trusted adapters and deployment assembly resolve secrets."""

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Read a connection including its encrypted secret."""
        ...

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        """Read a profile including its bundle and encrypted inputs."""
        ...


class ComputeSecretStore:
    """Encrypt secrets for storage and resolve stable references on demand."""

    def __init__(self, connections: ComputeConnectionReader, *, key: str | None = None) -> None:
        self._connections = connections
        self._key_override = key
        self._material = ""
        self._cipher: Fernet | None = None
        self._refresh_cipher()

    def _refresh_cipher(self) -> None:
        material = (
            self._key_override
            if self._key_override is not None
            else setting_env(ENV_COMPUTE_SECRET_KEY, "") or ""
        )
        if material == self._material:
            return
        self._cipher = None
        if material:
            if len(material.encode()) < 32:
                raise ComputeSecretError("compute encryption key must contain at least 32 bytes")
            digest = hashlib.sha256(_DOMAIN + b"\0" + material.encode()).digest()
            self._cipher = Fernet(base64.urlsafe_b64encode(digest))
        self._material = material

    @property
    def repository(self) -> ComputeConnectionReader:
        """Expose the trusted reader to the deployment resolver only."""
        return self._connections

    @property
    def configured(self) -> bool:
        """Whether credential writes and resolution can be safely performed."""
        self._refresh_cipher()
        return self._cipher is not None

    def encrypt(self, connection_id: str, provider_id: str, secret: str) -> str:
        """Bind a secret to its owner before authenticated encryption."""
        cipher = self._require_cipher()
        payload = json.dumps({"id": connection_id, "provider": provider_id, "secret": secret})
        return cipher.encrypt(payload.encode()).decode("ascii")

    def decrypt(self, connection_id: str, provider_id: str, ciphertext: str) -> str:
        """Verify identity binding as well as integrity before returning plaintext."""
        cipher = self._require_cipher()
        try:
            payload = json.loads(cipher.decrypt(ciphertext.encode("ascii")))
            if (
                not isinstance(payload, dict)
                or payload.get("id") != connection_id
                or payload.get("provider") != provider_id
                or not isinstance(payload.get("secret"), str)
            ):
                raise ValueError("invalid binding")
            return str(payload["secret"])
        except (InvalidToken, ValueError, UnicodeError, TypeError):
            raise ComputeSecretError("compute credential cannot be decrypted") from None

    async def resolve(self, reference: str) -> str:
        """Resolve a private key, API token, or explicitly verified host-key list."""
        connection_id, separator, field = reference.partition(":")
        if separator and field != "known_hosts":
            raise ComputeSecretError("unsupported compute credential reference")
        record = await self._connections.get_connection(connection_id)
        if record is None:
            raise ComputeSecretError("compute connection no longer exists")
        if field == "known_hosts":
            configuration = record.get("configuration") or {}
            known_hosts = configuration.get("known_hosts")
            if not isinstance(known_hosts, str) or not known_hosts.strip():
                raise ComputeSecretError("verified SSH host keys have not been configured")
            return known_hosts
        return self.decrypt(connection_id, record["provider_id"], record["secret_ciphertext"])

    def _require_cipher(self) -> Fernet:
        self._refresh_cipher()
        if self._cipher is None:
            raise ComputeSecretError("compute secret storage is not configured")
        return self._cipher


class StoredDeploymentResolver:
    """Resolve database-backed profile bundles and encrypted deployment inputs."""

    PREFIX = "stored-profile:"

    def __init__(self, repository: ComputeConnectionReader, secrets: ComputeSecretStore) -> None:
        self._repository = repository
        self._secrets = secrets

    async def bundle(self, reference: str) -> bytes:
        """Return the immutable source bundle for a stored profile reference."""
        profile_id = self._profile_id(reference, kind="bundle")
        row = await self._row(profile_id)
        value = row.get("source_bundle")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ComputeSecretError("deployment bundle is unavailable")
        return bytes(value)

    async def resolve(self, reference: str) -> str:
        """Decrypt one environment or credential-file profile reference."""
        parts = reference.split(":", 3)
        if len(parts) < 3 or parts[0] != "stored-profile":
            raise ComputeSecretError("deployment credential reference is not configured")
        profile_id, kind = parts[1], parts[2]
        row = await self._row(profile_id)
        plaintext = self._secrets.decrypt(
            "profile:" + profile_id, "deployment_profile", row["secret_ciphertext"]
        )
        try:
            payload = json.loads(plaintext)
            if kind == "environment" and isinstance(payload.get("environment"), str):
                return str(payload["environment"])
            if kind == "file" and len(parts) == 4:
                value = payload.get("files", {}).get(parts[3])
                if isinstance(value, str):
                    return value
        except (ValueError, TypeError):
            pass
        raise ComputeSecretError("deployment credential cannot be decrypted")

    async def _row(self, profile_id: str) -> dict[str, Any]:
        row = await self._repository.get_deployment_profile(profile_id)
        if row is None:
            raise ComputeSecretError("deployment profile no longer exists")
        return row

    @staticmethod
    def _profile_id(reference: str, *, kind: str) -> str:
        parts = reference.split(":")
        if len(parts) != 3 or parts[0] != "stored-profile" or parts[2] != kind:
            raise ComputeSecretError("deployment bundle reference is invalid")
        return parts[1]
