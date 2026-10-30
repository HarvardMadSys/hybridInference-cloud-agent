"""Keyed fingerprints of what an environment installation depends on.

Equal fingerprints install the same thing, so an installation snapshot made
for one binding serves another. They are keyed with the user credential key,
so a fingerprint written into a container tells its code nothing about the
values; that makes them lookup keys a key rotation must rebuild
(:mod:`cloud_agent.application.credential_key_rotation`). Until it has, a
stored fingerprint may be under either key, and :func:`setup_fingerprints`
lists both.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.environments import EnvironmentCipher, EnvironmentEncryptionError

if TYPE_CHECKING:
    from cloud_agent_shared.environment import EnvironmentVariable


def setup_material(setup_script: str | None, variables: list[EnvironmentVariable]) -> bytes:
    """The bytes a fingerprint is a MAC of: the script and the setup-time values."""
    return json.dumps(
        {
            "v": 1,
            "setup_script": setup_script or "",
            "variables": sorted([v.name, v.value] for v in variables if v.phase != "runtime"),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def setup_fingerprint(setup_script: str | None, variables: list[EnvironmentVariable]) -> str:
    """HMAC of what an installation depends on, under the current index key."""
    return EnvironmentCipher().mac(setup_material(setup_script, variables))


def setup_fingerprints(setup_script: str | None, variables: list[EnvironmentVariable]) -> list[str]:
    """Every fingerprint a stored row for this installation may hold during a key rotation."""
    return EnvironmentCipher().macs(setup_material(setup_script, variables))


def optional_setup_fingerprint(
    setup_script: str | None, variables: list[EnvironmentVariable]
) -> str | None:
    """The fingerprint, or ``None`` without setup or without the deployment's key.

    Snapshots need the key; tasks do not, so a deployment without it simply
    never snapshots.
    """
    if not setup_script:
        return None
    try:
        return setup_fingerprint(setup_script, variables)
    except EnvironmentEncryptionError:
        return None


def fingerprint_epoch() -> str:
    """Which key fingerprints are computed under now, for caches keyed by fingerprint."""
    try:
        return EnvironmentCipher().mac(b"fingerprint-epoch")[:16]
    except EnvironmentEncryptionError:
        return ""


__all__ = [
    "fingerprint_epoch",
    "optional_setup_fingerprint",
    "setup_fingerprint",
    "setup_fingerprints",
    "setup_material",
]
