"""Keep the control plane's deployment keys in their own volume, not in a ``.env``.

The values every control-plane process needs before it can read a setting live
as one file each in the ``agent-keys`` volume: the database connection, the
bundled database's password, the account system's key and the settings root
key (or the legacy ``AGENT_SESSION_SECRET`` root of an older deployment). The
Compose ``keys`` service runs this module before anything else starts:

* a value set in the environment (a deployment's ``.env`` or the deploying
  process) is stored, so an existing deployment can delete those lines after
  one start on this release;
* a value already stored is kept;
* a missing value is generated only on a provably new installation: the
  database is the bundled one and its volume holds no database yet. Anywhere
  else the start stops and names what to set, because the settings root key
  decrypts every stored secret and a new one would lock them all.

Each file is readable only by the user that needs it: the database password by
the Postgres image's ``postgres`` user, everything else by the control-plane
image's ``cloudagent`` user. The image's entrypoint
(``deploy/control-plane/load-keys.sh``) fills each variable the environment
leaves empty from its file. Nothing here prints a value, except ``export``,
which writes the keys an operator must back up apart from the database as
``.env`` lines on standard output; that text restores them on another machine.

See docs/agents/specs/2026-11-09-no-env-file.zh-CN.md.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Where the ``keys`` service and every consumer mount the ``agent-keys`` volume.
KEYS_DIR = Path("/run/cloud-agent/keys")
#: Where the ``keys`` service mounts the bundled database's volume, read-only.
DATABASE_DIR = Path("/run/cloud-agent/database")

DATABASE_URL = "AGENT_DATABASE_URL"
POSTGRES_PASSWORD = "AGENT_POSTGRES_PASSWORD"
LOCAL_SESSION_SECRET = "AGENT_LOCAL_SESSION_SECRET"
SETTINGS_ROOT = "AGENT_SETTINGS_ENCRYPTION_KEY"
#: An older deployment's settings root. Stored when set, never generated: a
#: dedicated key beside it would replace that root.
LEGACY_SETTINGS_ROOT = "AGENT_SESSION_SECRET"

#: Every value this volume holds, in the order ``export`` writes them.
STORED = (
    DATABASE_URL,
    POSTGRES_PASSWORD,
    LOCAL_SESSION_SECRET,
    SETTINGS_ROOT,
    LEGACY_SETTINGS_ROOT,
)
#: Without these no control-plane process starts.
REQUIRED = (DATABASE_URL, POSTGRES_PASSWORD, LOCAL_SESSION_SECRET)

#: ``postgres:16-alpine`` runs as uid and gid 70; the control-plane image as 10001.
POSTGRES_UID = 70
CONTROL_PLANE_UID = 10001
_OWNERS = {POSTGRES_PASSWORD: POSTGRES_UID}

_BUNDLED_HOST = re.compile(r"@postgres(:[0-9]+)?(/|$)")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


class KeysRefused(RuntimeError):
    """A key is missing and cannot be generated without risking stored data."""


@dataclass
class Outcome:
    """What one run stored, without the values."""

    stored: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _new_secret() -> str:
    return secrets.token_hex(32)


def _read(directory: Path, name: str) -> str:
    """One stored value, byte for byte: a key's spaces are part of the key."""
    try:
        return (directory / name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _given(environ: Mapping[str, str], name: str) -> str:
    """The environment's value as it is; empty or only spaces counts as unset."""
    value = environ.get(name) or ""
    return value if value.strip() else ""


def _write(directory: Path, name: str, value: str) -> None:
    """Replace one key file atomically, readable only by the user that needs it."""
    if not value or any(char in value for char in "\n\r\0"):
        raise KeysRefused(f"{name} must be a single line.")
    temporary = directory / f".{name}.new"
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    if os.geteuid() == 0:
        owner = _OWNERS.get(name, CONTROL_PLANE_UID)
        os.chown(temporary, owner, owner)
    os.replace(temporary, directory / name)


def bundled(url: str) -> bool:
    """Whether ``url`` names this stack's own ``postgres`` service."""
    return bool(_BUNDLED_HOST.search(url))


def not_new_reason(url: str, database: Path) -> str:
    """Why this cannot be proved a new installation; empty when it can."""
    if url and not bundled(url):
        return f"{DATABASE_URL} names a database other than the bundled one"
    try:
        holds_database = (database / "PG_VERSION").exists()
    except OSError:
        return "the bundled database's volume could not be read"
    if holds_database:
        return "the bundled database's volume already holds a database"
    return ""


def _bundled_password(url: str) -> str:
    try:
        password = urlsplit(url).password
    except ValueError:
        return ""
    return unquote(password) if password else ""


def _bundled_url(environ: Mapping[str, str], password: str) -> str:
    user = (environ.get("AGENT_POSTGRES_USER") or "").strip() or "cloudagent"
    database = (environ.get("AGENT_POSTGRES_DB") or "").strip() or "cloudagent"
    if not (_IDENTIFIER.fullmatch(user) and _IDENTIFIER.fullmatch(database)):
        raise KeysRefused(
            "AGENT_POSTGRES_USER and AGENT_POSTGRES_DB must be letters, digits and _ "
            f"for {DATABASE_URL} to be generated; set {DATABASE_URL} yourself."
        )
    # The password file keeps the password itself; the connection carries it
    # percent-encoded, so `/`, `?`, `#` or `@` in it cannot move the host.
    return f"postgresql://{user}:{quote(password, safe='')}@postgres:5432/{database}"


def _collect(environ: Mapping[str, str], keys: Path, outcome: Outcome) -> dict[str, str]:
    """The environment's values over the stored ones; stores what changed."""
    values: dict[str, str] = {}
    for name in STORED:
        given = _given(environ, name)
        kept = _read(keys, name)
        if given and given != kept:
            _write(keys, name, given)
            outcome.stored.append(name)
        if given or kept:
            values[name] = given or kept
    return values


def _complete_password(values: dict[str, str], keys: Path, outcome: Outcome) -> None:
    """Give the bundled database the password its connection already names.

    With a database elsewhere the bundled one serves nothing, and only needs a
    password to start, so any new one will do.
    """
    url = values.get(DATABASE_URL, "")
    if not url or values.get(POSTGRES_PASSWORD):
        return
    if bundled(url):
        password = _bundled_password(url)
        if not password:
            raise KeysRefused(
                f"{DATABASE_URL} names the bundled database without a password: "
                f"set {POSTGRES_PASSWORD} to the password it was created with."
            )
    else:
        password = _new_secret()
    _write(keys, POSTGRES_PASSWORD, password)
    values[POSTGRES_PASSWORD] = password
    outcome.stored.append(POSTGRES_PASSWORD)


def _generate(
    environ: Mapping[str, str], values: dict[str, str], keys: Path, outcome: Outcome
) -> None:
    """Fill every missing value on a new installation."""
    fresh: dict[str, str] = {}
    if not values.get(POSTGRES_PASSWORD):
        fresh[POSTGRES_PASSWORD] = _new_secret()
    if not values.get(DATABASE_URL):
        password = fresh.get(POSTGRES_PASSWORD) or values[POSTGRES_PASSWORD]
        fresh[DATABASE_URL] = _bundled_url(environ, password)
    if not values.get(LOCAL_SESSION_SECRET):
        fresh[LOCAL_SESSION_SECRET] = _new_secret()
    if not values.get(SETTINGS_ROOT) and not values.get(LEGACY_SETTINGS_ROOT):
        fresh[SETTINGS_ROOT] = _new_secret()
    for name in STORED:
        if name in fresh:
            _write(keys, name, fresh[name])
            values[name] = fresh[name]
            outcome.generated.append(name)


def prepare(environ: Mapping[str, str], keys: Path, database: Path) -> Outcome:
    """Store, keep or generate every deployment key; see the module docstring.

    Raises:
        KeysRefused: A required key is missing where data may already depend
            on it, or the values given contradict each other.
    """
    outcome = Outcome()
    values = _collect(environ, keys, outcome)
    _complete_password(values, keys, outcome)
    missing = [name for name in REQUIRED if not values.get(name)]
    no_root = not values.get(SETTINGS_ROOT) and not values.get(LEGACY_SETTINGS_ROOT)
    if not missing and not no_root:
        return outcome
    reason = not_new_reason(values.get(DATABASE_URL, ""), database)
    if not reason:
        _generate(environ, values, keys, outcome)
        return outcome
    if missing:
        raise KeysRefused(
            f"{', '.join(missing)} not set, and not generated: {reason}. Set them in the "
            "deployment's environment (its .env), with exactly the values this database "
            "was created with, never new ones; they are stored on the next start."
        )
    outcome.warnings.append(
        f"{SETTINGS_ROOT} is not set and was not generated: {reason}. Set the key this "
        "deployment's settings were saved with."
    )
    return outcome


def export(keys: Path) -> list[str]:
    """The stored keys this process can read, as ``.env`` lines that restore them exactly.

    Each value is single-quoted: Compose reads a single-quoted ``.env`` value
    literally, so a ``$`` or a space in a key survives the round trip, where an
    unquoted one would be interpolated or trimmed.

    Raises:
        KeysRefused: A value holds a single quote, which no ``.env`` quoting
            Compose reads literally can carry.
    """
    lines = []
    for name in STORED:
        try:
            value = _read(keys, name)
        except PermissionError:
            # The database password is the Postgres user's; the bundled
            # connection carries it, so the export restores it all the same.
            continue
        if not value:
            continue
        if "'" in value:
            raise KeysRefused(
                f"{name} holds a single quote, which a .env line cannot carry literally; "
                "back up the agent-keys volume itself instead."
            )
        lines.append(f"{name}='{value}'")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``prepare`` (the ``keys`` service) or ``export`` (a backup)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", nargs="?", choices=("prepare", "export"), default="prepare")
    parser.add_argument("--keys-dir", type=Path, default=KEYS_DIR)
    parser.add_argument("--database-dir", type=Path, default=DATABASE_DIR)
    arguments = parser.parse_args(argv)
    if arguments.command == "export":
        try:
            lines = export(arguments.keys_dir)
        except KeysRefused as refused:
            print(f"[keys] ERROR: {refused}", file=sys.stderr)
            return 1
        for line in lines:
            print(line)
        return 0
    try:
        outcome = prepare(os.environ, arguments.keys_dir, arguments.database_dir)
    except KeysRefused as refused:
        print(f"[keys] ERROR: {refused}", file=sys.stderr)
        return 1
    for warning in outcome.warnings:
        print(f"[keys] WARNING: {warning}", file=sys.stderr)
    if outcome.stored:
        print(f"[keys] stored from the environment: {' '.join(outcome.stored)}")
    if outcome.generated:
        print(
            f"[keys] new installation: generated {' '.join(outcome.generated)}; back them up "
            "apart from the database (docs/developer/disaster-recovery.md)"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the keys service
    sys.exit(main())
