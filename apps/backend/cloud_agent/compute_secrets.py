"""Encrypted, write-only compute credentials resolved inside the control plane.

The encryption key is separate from gateway, host and sandbox credentials.
Ciphertexts are bound to both connection identity and provider, so swapping
database values cannot turn one administrator connection into another.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

ENV_COMPUTE_SECRET_KEY = "AGENT_COMPUTE_SECRET_KEY"
_DOMAIN = b"cloud-agent/compute-credentials/v1"


class ComputeSecretError(Exception):
    """A safe error whose message contains no credential or ciphertext."""


class ComputeConnectionReader(Protocol):
    """Only trusted adapters and deployment assembly resolve secrets."""

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Read a connection including its encrypted secret."""
        ...


class ComputeSecretStore:
    """Encrypt secrets for storage and resolve stable references on demand."""

    def __init__(self, connections: ComputeConnectionReader, *, key: str | None = None) -> None:
        self._connections = connections
        material = os.environ.get(ENV_COMPUTE_SECRET_KEY, "") if key is None else key
        self._cipher: Fernet | None = None
        if material:
            if len(material.encode()) < 32:
                raise ComputeSecretError("compute encryption key must contain at least 32 bytes")
            digest = hashlib.sha256(_DOMAIN + b"\0" + material.encode()).digest()
            self._cipher = Fernet(base64.urlsafe_b64encode(digest))

    @property
    def configured(self) -> bool:
        """Whether credential writes and resolution can be safely performed."""
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
        if self._cipher is None:
            raise ComputeSecretError("compute secret storage is not configured")
        return self._cipher
