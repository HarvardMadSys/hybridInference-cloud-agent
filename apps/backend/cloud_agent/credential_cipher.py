"""Domain-separated authenticated encryption for stored credentials.

A cipher holds a key ring of at most two keys. It always encrypts with the
current key and decrypts with the current key, then the previous one, which a
key rotation keeps until it has re-encrypted every stored value. Lookup
digests (:meth:`CredentialCipher.mac`) use the *index* key: the previous key
until the rotation rebuilds the stored digests, the current one afterwards.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import InvalidToken

from cloud_agent.domain_cipher import domain_fernet

# This deployed name is retained for compatibility even though the key protects
# more than source-control credentials.
USER_CREDENTIAL_ENCRYPTION_KEY = "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"
COMPUTE_CREDENTIAL_ENCRYPTION_KEY = "AGENT_COMPUTE_SECRET_KEY"


class CredentialCipherError(Exception):
    """A credential key is absent or ciphertext cannot be authenticated."""


def _mac_key(domain: bytes, secret: str) -> bytes:
    return hashlib.sha256(domain + b"\0mac\0" + secret.encode("utf-8")).digest()


class CredentialCipher:
    """Encrypt and authenticate values under one explicit credential domain."""

    def __init__(
        self,
        secret: str,
        *,
        domain: bytes,
        previous: str = "",
        index_previous: bool = False,
    ) -> None:
        """Derive independent encryption and MAC keys without setting key policy.

        ``previous`` only decrypts; ``index_previous`` makes :meth:`mac` use it.
        """
        if not secret:
            raise CredentialCipherError("credential encryption key is not configured")
        self._fernet = domain_fernet(secret, domain)
        self._mac_key = _mac_key(domain, secret)
        self._previous_fernet = None
        self._previous_mac_key: bytes | None = None
        if previous and previous != secret:
            self._previous_fernet = domain_fernet(previous, domain)
            self._previous_mac_key = _mac_key(domain, previous)
        self._index_previous = index_previous and self._previous_mac_key is not None

    @property
    def rotating(self) -> bool:
        """Whether a previous key is held for decrypting."""
        return self._previous_fernet is not None

    def encrypt(self, value: str) -> str:
        """Return authenticated ciphertext; plaintext is never persisted."""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def mac(self, value: bytes) -> str:
        """Return a stable keyed digest under the index key, without retaining the input."""
        key = self._previous_mac_key if self._index_previous else self._mac_key
        return hmac.new(key or self._mac_key, value, hashlib.sha256).hexdigest()

    def macs(self, value: bytes) -> list[str]:
        """Every digest a stored lookup key may still hold: the index key's first."""
        digests = [self.mac(value)]
        for key in (self._mac_key, self._previous_mac_key):
            if key is not None:
                digest = hmac.new(key, value, hashlib.sha256).hexdigest()
                if digest not in digests:
                    digests.append(digest)
        return digests

    def mac_with_current(self, value: bytes) -> str:
        """The digest under the current key, whatever the index key is (for re-indexing)."""
        return hmac.new(self._mac_key, value, hashlib.sha256).hexdigest()

    def mac_with_previous(self, value: bytes) -> str | None:
        """The digest under the previous key, or ``None`` outside a rotation."""
        if self._previous_mac_key is None:
            return None
        return hmac.new(self._previous_mac_key, value, hashlib.sha256).hexdigest()

    def decrypt(self, value: str) -> str:
        """Decrypt a persisted secret and fail closed on corruption."""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            if self._previous_fernet is not None:
                try:
                    return self._previous_fernet.decrypt(value.encode("ascii")).decode("utf-8")
                except (InvalidToken, UnicodeError, ValueError):
                    pass
            raise CredentialCipherError("stored credential could not be decrypted") from exc

    def opens_with_current(self, value: str) -> bool:
        """Whether ``value`` is already encrypted with the current key."""
        try:
            self._fernet.decrypt(value.encode("ascii"))
        except (InvalidToken, UnicodeError, ValueError):
            return False
        return True


def user_key_ring() -> tuple[str, str, bool]:
    """The user credential key, the previous one a rotation keeps, and the index choice."""
    from cloud_agent.settings import boolean_setting, setting_env, text_setting

    return (
        setting_env(USER_CREDENTIAL_ENCRYPTION_KEY, "") or "",
        text_setting("source_control_encryption_key_previous") or "",
        boolean_setting("credential_index_uses_previous"),
    )


def compute_key_ring() -> tuple[str, str]:
    """The compute credential key and the previous one a rotation keeps."""
    from cloud_agent.settings import setting_env, text_setting

    return (
        setting_env(COMPUTE_CREDENTIAL_ENCRYPTION_KEY, "") or "",
        text_setting("compute_encryption_key_previous") or "",
    )


def user_credential_cipher(domain: bytes, secret: str | None = None) -> CredentialCipher:
    """The cipher for one user-credential domain: an explicit key, or the saved key ring."""
    if secret is not None:
        return CredentialCipher(secret, domain=domain)
    current, previous, index_previous = user_key_ring()
    return CredentialCipher(
        current, domain=domain, previous=previous, index_previous=index_previous
    )


__all__ = [
    "COMPUTE_CREDENTIAL_ENCRYPTION_KEY",
    "USER_CREDENTIAL_ENCRYPTION_KEY",
    "CredentialCipher",
    "CredentialCipherError",
    "compute_key_ring",
    "user_credential_cipher",
    "user_key_ring",
]
