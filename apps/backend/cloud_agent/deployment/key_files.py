"""Read the deployment keys a control-plane process needs from the ``agent-keys`` volume.

The Compose ``keys`` service (:mod:`cloud_agent.deployment.deployment_keys`)
keeps one file per key in the volume, mounted read-only at :data:`KEYS_DIR` in
every control-plane container. :func:`fill_environment` runs when the
``cloud_agent`` package is imported, so every Python process of the image sees
the keys however it was started: as the service, by ``compose run``, by
``compose exec`` or by a healthcheck. Neither of the last two goes through an
image entrypoint, which is why this is not one.

Kept apart from ``deployment_keys`` so the package import loads only this.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

#: Where the ``keys`` service and every consumer mount the ``agent-keys`` volume.
KEYS_DIR = Path("/run/cloud-agent/keys")

DATABASE_URL = "AGENT_DATABASE_URL"
LOCAL_SESSION_SECRET = "AGENT_LOCAL_SESSION_SECRET"
SETTINGS_ROOT = "AGENT_SETTINGS_ENCRYPTION_KEY"
#: An older deployment's settings root.
LEGACY_SETTINGS_ROOT = "AGENT_SESSION_SECRET"

#: What a control-plane process reads from the volume. The bundled database's
#: password is not among them: it is the Postgres user's file, and the
#: connection carries it.
FILLED = (DATABASE_URL, LOCAL_SESSION_SECRET, SETTINGS_ROOT, LEGACY_SETTINGS_ROOT)


def read(directory: Path, name: str) -> str:
    """One stored value, byte for byte: a key's spaces are part of the key."""
    try:
        return (directory / name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def given(environ: Mapping[str, str], name: str) -> str:
    """The environment's value as it is; empty or only spaces counts as unset."""
    value = environ.get(name) or ""
    return value if value.strip() else ""


def fill_environment(environ: MutableMapping[str, str], keys: Path = KEYS_DIR) -> list[str]:
    """Set each key the environment leaves unset from its stored file.

    An environment value still wins. Without the volume (a development
    checkout, the tests, another image) nothing changes. Returns the names
    filled, never their values.
    """
    if not keys.is_dir():
        return []
    filled = []
    for name in FILLED:
        if given(environ, name):
            continue
        try:
            value = read(keys, name)
        except OSError:
            continue
        if value:
            environ[name] = value
            filled.append(name)
    return filled
