"""Domain-separated authenticated encryption for stored credentials."""

from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import InvalidToken

from cloud_agent.domain_cipher import domain_fernet

# This deployed name is retained for compatibility even though the key protects
# more than source-control credentials.
USER_CREDENTIAL_ENCRYPTION_KEY = "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"


class CredentialCipherError(Exception):
    """A credential key is absent or ciphertext cannot be authenticated."""


class CredentialCipher:
    """Encrypt and authenticate values under one explicit credential domain."""

    def __init__(self, secret: str, *, domain: bytes) -> None:
        """Derive independent encryption and MAC keys without setting key policy."""
        if not secret:
            raise CredentialCipherError("credential encryption key is not configured")
        material = secret.encode("utf-8")
        self._fernet = domain_fernet(secret, domain)
        self._mac_key = hashlib.sha256(domain + b"\0mac\0" + material).digest()

    def encrypt(self, value: str) -> str:
        """Return authenticated ciphertext; plaintext is never persisted."""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def mac(self, value: bytes) -> str:
        """Return a stable keyed digest without retaining the input."""
        return hmac.new(self._mac_key, value, hashlib.sha256).hexdigest()

    def decrypt(self, value: str) -> str:
        """Decrypt a persisted secret and fail closed on corruption."""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialCipherError("stored credential could not be decrypted") from exc


__all__ = ["USER_CREDENTIAL_ENCRYPTION_KEY", "CredentialCipher", "CredentialCipherError"]
