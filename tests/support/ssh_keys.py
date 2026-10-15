"""Throwaway SSH keys made at test time.

Generated rather than written into the tests: a key-shaped literal in a
tracked file is what secret scanning looks for, and these are only fixtures.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def host_key() -> str:
    """The base64 blob of a fresh Ed25519 public key, as ``known_hosts`` holds it."""
    public = ed25519.Ed25519PrivateKey.generate().public_key()
    line = public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return line.decode().split()[1]


def private_key() -> str:
    """A fresh Ed25519 private key in OpenSSH format."""
    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )


def parses_as_ed25519(text: str) -> bool:
    """Whether ``text`` loads as an unencrypted OpenSSH Ed25519 private key."""
    try:
        key = serialization.load_ssh_private_key(text.encode(), password=None)
    except ValueError:
        return False
    return isinstance(key, ed25519.Ed25519PrivateKey)
