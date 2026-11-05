"""Keys for encrypting stored credentials, one per domain, from one server secret.

Each kind of stored credential derives its key under its own domain: one
secret to deploy and rotate, while a ciphertext from one table cannot be
decrypted, or planted, as another's.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def domain_fernet(secret: str, domain: bytes) -> Fernet:
    """The authenticated cipher for ``domain``, derived from ``secret``."""
    digest = hashlib.sha256(domain + b"\0" + secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def settings_root_key() -> str:
    """The secret that saved settings are encrypted under.

    ``AGENT_SETTINGS_ENCRYPTION_KEY``, or the session secret where it is not
    set. Callers hold it to their own minimum length.
    """
    return (
        os.environ.get("AGENT_SETTINGS_ENCRYPTION_KEY")
        or os.environ.get("AGENT_SESSION_SECRET")
        or ""
    )
