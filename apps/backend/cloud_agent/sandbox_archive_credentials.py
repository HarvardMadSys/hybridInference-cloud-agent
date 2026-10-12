"""Encrypt the optional sandbox-archive access key at rest.

The sandbox manager needs the secret in memory. The database stores only ciphertext
under the settings encryption key. Plaintext never stays in the JSONB column,
and a missing key fails closed instead of writing the secret unprotected.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from cloud_agent.source_control import SourceControlCipher, SourceControlError
from cloud_agent_shared.sandbox_archive_settings import (
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_DOMAIN = b"cloud-agent/sandbox-archive-aws/v1"
_SECRET = "aws_secret_access_key"


def _cipher() -> SourceControlCipher:
    secret = (
        os.environ.get("AGENT_SETTINGS_ENCRYPTION_KEY")
        or os.environ.get("AGENT_SESSION_SECRET")
        or ""
    )
    if len(secret.encode()) < 32:
        raise SandboxArchivePolicyError(
            "A settings encryption key of at least 32 bytes is required to store an AWS secret access key"
        )
    try:
        return SourceControlCipher(secret, domain=_DOMAIN)
    except SourceControlError as exc:
        raise SandboxArchivePolicyError(str(exc)) from exc


def open_archive_secret(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Decrypt a stored secret, rejecting plaintext at rest."""
    values = dict(raw or {})
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
            values[_SECRET] = _cipher().decrypt(secret["ciphertext"])
        except SourceControlError as exc:
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


def load_stored_archive_policy(raw: Mapping[str, Any] | None) -> SandboxArchivePolicy:
    """Parse one database object after decrypting its access key."""
    return SandboxArchivePolicy.from_mapping(open_archive_secret(raw))


def archive_settings_payload(
    policy: SandboxArchivePolicy,
    updated_at: Any,
    **extra: Any,
) -> dict[str, Any]:
    """Return the shared read model: sandbox manager fields plus the admin configured flag."""
    payload = policy.as_sandbox_manager_dict()
    payload["aws_secret_access_key_configured"] = bool(policy.aws_secret_access_key)
    payload["updated_at"] = updated_at
    payload.update(extra)
    return payload
