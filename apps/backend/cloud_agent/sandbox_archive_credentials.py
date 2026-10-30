"""Encrypt the optional sandbox-archive access key at rest.

The sandbox manager needs the secret in memory. The database stores only ciphertext
under the settings encryption key. Plaintext never stays in the JSONB column,
and a missing key fails closed instead of writing the secret unprotected.

A stored policy is read through :func:`stored_archive_policy` alone, which
opens the key before it parses. ``SandboxArchivePolicy.from_mapping`` refuses
the sealed key, so a reader that parsed the stored settings itself refused
every policy with a saved key (#984).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cloud_agent.credential_cipher import CredentialCipher, CredentialCipherError
from cloud_agent.domain_cipher import decrypt_with_root, settings_root_key
from cloud_agent_shared.sandbox_archive_settings import (
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)

_DOMAIN = b"cloud-agent/sandbox-archive-aws/v1"
_SECRET = "aws_secret_access_key"


def _cipher() -> CredentialCipher:
    secret = settings_root_key()
    if len(secret.encode()) < 32:
        raise SandboxArchivePolicyError(
            "A settings encryption key of at least 32 bytes is required to store an AWS secret access key"
        )
    try:
        return CredentialCipher(secret, domain=_DOMAIN)
    except CredentialCipherError as exc:
        raise SandboxArchivePolicyError(str(exc)) from exc


def open_archive_secret(raw: Mapping[str, Any], *, previous_allowed: bool = True) -> dict[str, Any]:
    """Decrypt a stored archive policy's secret; ``previous_allowed=False`` proves a re-encryption."""
    if previous_allowed:
        return _open_archive_secret(raw)
    values = dict(raw)
    secret = values.get(_SECRET)
    if isinstance(secret, dict) and "ciphertext" in secret:
        try:
            values[_SECRET] = _cipher().decrypt(secret["ciphertext"])
        except CredentialCipherError as exc:
            raise SandboxArchivePolicyError("the re-encrypted access key does not open") from exc
    return values


def _open_archive_secret(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Decrypt a stored secret, rejecting plaintext at rest."""
    values = dict(raw)
    secret = values.get(_SECRET)
    if secret in (None, ""):
        values.pop(_SECRET, None)
        return values
    if (
        isinstance(secret, dict)
        and set(secret) == {"ciphertext"}
        and isinstance(secret.get("ciphertext"), str)
    ):
        try:
            values[_SECRET] = decrypt_with_root(
                lambda key: CredentialCipher(key, domain=_DOMAIN), secret["ciphertext"]
            )
        except CredentialCipherError as exc:
            raise SandboxArchivePolicyError(
                "stored AWS secret access key could not be decrypted"
            ) from exc
        return values
    raise SandboxArchivePolicyError("stored AWS secret access key is not encrypted")


def seal_archive_storage(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-ready copy whose secret, if any, is ciphertext."""
    sealed = dict(values)
    secret = sealed.get(_SECRET) or ""
    if not secret:
        sealed.pop(_SECRET, None)
        return sealed
    if not isinstance(secret, str):
        raise SandboxArchivePolicyError("AWS secret access key must be a string")
    sealed[_SECRET] = {"ciphertext": _cipher().encrypt(secret)}
    return sealed


def stored_archive_policy(row: Mapping[str, Any] | None) -> SandboxArchivePolicy:
    """The archive policy an ``agent_runner_policy`` row stores, its access key opened.

    The row holds the ``sandbox_archive_settings`` object and the
    ``sandbox_archive_revision`` it is at. A missing row stores the default
    policy. A policy that does not parse, or whose key does not open, raises
    :class:`SandboxArchivePolicyError`, and each caller decides what that means
    for it.
    """
    if row is None:
        return SandboxArchivePolicy()
    stored = row["sandbox_archive_settings"]
    if isinstance(stored, (str, bytes)):
        # asyncpg returns a JSONB column as its text.
        stored = json.loads(stored)
    if not isinstance(stored, Mapping):
        raise SandboxArchivePolicyError("stored sandbox archive settings are not an object")
    return SandboxArchivePolicy.from_mapping(
        {**_open_archive_secret(stored), "revision": row["sandbox_archive_revision"]}
    )


def archive_settings_payload(
    policy: SandboxArchivePolicy,
    updated_at: Any,
    **extra: Any,
) -> dict[str, Any]:
    """Return the shared read model: sandbox manager fields plus the admin configured flag."""
    payload = policy.as_sandbox_manager_dict()
    # The wire form omits the default; admins and the heartbeat model read
    # the resolved value.
    payload["checkpoints_enabled"] = policy.checkpoints_enabled
    payload["aws_secret_access_key_configured"] = bool(policy.aws_secret_access_key)
    payload["updated_at"] = updated_at
    payload.update(extra)
    return payload
